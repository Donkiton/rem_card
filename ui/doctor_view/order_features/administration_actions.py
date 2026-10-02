"""AdministrationActionsMixin for the doctor orders widget."""

from PySide6.QtCore import Qt
from copy import copy
from datetime import datetime
from datetime import timedelta
from rem_card.app.logger import logger
from rem_card.data.dto.remcard_dto import AdministrationDTO
from rem_card.data.dto.remcard_dto import OrderDTO
from rem_card.ui.doctor_view.order_features.constants import ORDERS_CELL_REPEAT_GUARD_SEC
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rem_card.ui.doctor_view.orders_widget import OrdersWidget


class _OptimisticAdminChanges:
    def __init__(self, widget: "OrdersWidget", op_prefix: str):
        self.widget = widget
        self.op_prefix = op_prefix
        self.previous_by_key: dict = {}
        self.changed_keys: list = []

    def _remember(self, item_key) -> None:
        if item_key in self.previous_by_key:
            return
        admin_map = self.widget.model.admin_map
        had_previous = item_key in admin_map
        self.previous_by_key[item_key] = (
            had_previous,
            copy(admin_map[item_key]) if had_previous else None,
        )

    def set_admin(self, item_key, next_admin) -> None:
        admin_map = self.widget.model.admin_map
        if admin_map.get(item_key) == next_admin:
            return
        self._remember(item_key)
        if next_admin is not None:
            next_admin.is_committed = 0
            setattr(next_admin, "_pending_cell_action", self.op_prefix)
        admin_map[item_key] = next_admin
        self.changed_keys.append(item_key)

    def remove_admin(self, item_key) -> None:
        admin_map = self.widget.model.admin_map
        if item_key not in admin_map:
            return
        self._remember(item_key)
        existing = admin_map.get(item_key)
        existing_status = str(getattr(existing, "status", "") or "")
        if (
            existing is not None
            and existing_status == "planned"
            and self.widget._is_committed_value(getattr(existing, "is_committed", 0))
        ):
            tombstone = copy(existing)
            tombstone.status = "deleted"
            tombstone.is_committed = 0
            tombstone.comment = ""
            tombstone.actual_time = None
            setattr(tombstone, "_pending_cell_action", self.op_prefix)
            admin_map[item_key] = tombstone
        else:
            del admin_map[item_key]
        self.changed_keys.append(item_key)


class AdministrationActionsMixin:
    def _schedule_fast_sync(self):
        self._fast_sync_timer.start(self._silent_sync_delay_ms)

    def _schedule_state_sync(self, delay_ms: int = 120):
        self._state_sync_timer.start(delay_ms)

    def _begin_admin_write(self):
        self._pending_admin_write_count += 1

    def _finish_admin_write(self):
        self._pending_admin_write_count = max(0, self._pending_admin_write_count - 1)

    @staticmethod
    def _admin_cell_write_key(order_id, planned_time):
        if order_id is None or planned_time is None:
            return None
        try:
            normalized_order_id = int(order_id)
        except Exception:
            normalized_order_id = order_id
        planned_key = planned_time.isoformat() if hasattr(planned_time, "isoformat") else str(planned_time)
        return (normalized_order_id, planned_key)

    def _prune_recent_admin_cell_clicks(self):
        if not self._recent_admin_cell_clicks:
            return
        cutoff = time.monotonic() - ORDERS_CELL_REPEAT_GUARD_SEC
        for key, clicked_at in list(self._recent_admin_cell_clicks.items()):
            if float(clicked_at or 0.0) < cutoff:
                self._recent_admin_cell_clicks.pop(key, None)

    def _skip_reason_for_admin_cell_click(self, key) -> str:
        if key is None:
            return ""
        if key in self._pending_admin_cell_write_keys:
            return "pending_write"
        self._prune_recent_admin_cell_clicks()
        clicked_at = self._recent_admin_cell_clicks.get(key)
        if clicked_at is not None and (time.monotonic() - float(clicked_at or 0.0)) < ORDERS_CELL_REPEAT_GUARD_SEC:
            return "repeat_click"
        return ""

    def _mark_admin_cell_click_accepted(self, key):
        if key is not None:
            self._recent_admin_cell_clicks[key] = time.monotonic()

    def _mark_pending_admin_cell_writes(self, keys):
        for key in keys or ():
            if key is not None:
                self._pending_admin_cell_write_keys.add(key)

    def _clear_pending_admin_cell_writes(self, keys):
        for key in keys or ():
            self._pending_admin_cell_write_keys.discard(key)

    def _run_fast_sync(self):
        """
        После optimistic update делаем один отложенный тихий snapshot-refresh.
        UI уже обновился локально, а source-of-truth подтягивается вне GUI-потока.
        """
        t0 = time.perf_counter() if self._perf_enabled else None
        try:
            logger.info(
                "[OrdersClick] silent_sync_start role=doctor admission_id=%s pending=%s",
                self.admission_id,
                self._pending_admin_write_count,
            )
            if self._pending_admin_write_count > 0:
                self._schedule_fast_sync()
                return
            self._request_snapshot(
                force=False,
                source="local_silent_sync",
                priority="LOW",
                invalidate_reason=None,
            )
            self._schedule_state_sync()
        except Exception:
            logger.info("[OrdersClick] silent_sync_exception role=doctor admission_id=%s", self.admission_id)
        finally:
            if self._perf_enabled and t0 is not None:
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                logger.debug(f"[OrdersPerf] silent_sync +{elapsed_ms:.1f}ms")

    def _on_cell_write_failed(self, _exc: Exception):
        # На ошибке возвращаемся к source-of-truth из БД.
        self.request_refresh(force=True)

    def _emit_admin_cell_changes(self, changed_keys, *, mark_draft: bool = True):
        if self.model is None or not changed_keys:
            return
        changed_keys = list(dict.fromkeys(changed_keys))
        if mark_draft:
            self._mark_local_admin_dirty(changed_keys)
        if hasattr(self.model, "_recompute_draft_flag"):
            self.model._recompute_draft_flag(emit_order_column=True)
        self._cached_has_drafts = bool(getattr(self.model, "has_any_draft", False))
        self._cached_has_administrations = self._model_has_administrations()
        if hasattr(self.model, "_emit_admin_cell_changes"):
            self.model._emit_admin_cell_changes(changed_keys)
        else:
            for key in changed_keys:
                row = next(
                    (
                        row_idx
                        for row_idx, item in enumerate(getattr(self.model, "orders", []))
                        if item and getattr(item, "id", None) == key[0]
                    ),
                    None,
                )
                col = next(
                    (
                        col_idx + 1
                        for col_idx, slot in enumerate(getattr(self.model, "time_slots", []))
                        if slot.isoformat() == key[1]
                    ),
                    None,
                )
                if row is not None and col is not None:
                    idx = self.model.index(row, col)
                    self.model.dataChanged.emit(idx, idx, [Qt.UserRole])
        self.check_drafts()
        self.localBalanceChanged.emit()

    def _restore_admin_cells(self, previous_by_key: dict):
        if self.model is None or not previous_by_key:
            return
        changed_keys = []
        for key, previous in previous_by_key.items():
            had_previous, previous_admin = previous
            if had_previous and previous_admin is not None:
                self.model.admin_map[key] = copy(previous_admin)
            else:
                self.model.admin_map.pop(key, None)
            changed_keys.append(key)
        self._mark_local_admin_dirty(changed_keys)
        self._emit_admin_cell_changes(changed_keys, mark_draft=False)

    @staticmethod
    def _admin_key_from_admin(admin):
        if admin is None:
            return None
        planned_time = getattr(admin, "planned_time", None)
        if isinstance(planned_time, str):
            try:
                planned_time = datetime.fromisoformat(planned_time)
            except Exception:
                return None
        order_id = getattr(admin, "order_id", None)
        if planned_time is None or order_id is None:
            return None
        return (order_id, planned_time.isoformat())

    def _apply_pending_order_mark(self, index, admin, mark: str) -> dict:
        if not self.model or not index.isValid() or admin is None:
            return {}
        key = self._admin_key_from_admin(admin)
        if key is None:
            return {}
        previous = copy(self.model.admin_map.get(key)) if key in self.model.admin_map else None
        pending_admin = copy(admin)
        actual_time = datetime.now() if mark else None
        pending_admin.comment = mark or ""
        pending_admin.actual_time = actual_time
        setattr(pending_admin, "_pending_mark", mark or "")
        self.model.admin_map[key] = pending_admin
        self._balance_mark_override_seq += 1
        self._balance_mark_overrides[int(getattr(admin, "id", 0) or 0)] = {
            "mark": mark or "",
            "actual_time": actual_time,
            "sequence": self._balance_mark_override_seq,
            "pending": True,
        }
        self._emit_admin_cell_changes([key], mark_draft=False)
        return {key: (previous is not None, previous)}

    def balance_mark_overrides(self) -> dict:
        return {
            int(admin_id): dict(value)
            for admin_id, value in self._balance_mark_overrides.items()
        }

    def balance_mark_override_sequence(self) -> int:
        pending = [int(value["sequence"]) for value in self._balance_mark_overrides.values() if value.get("pending")]
        return min(pending) - 1 if pending else int(self._balance_mark_override_seq or 0)

    def acknowledge_balance_mark_overrides(self, through_sequence: int) -> None:
        try:
            limit = int(through_sequence or 0)
        except (TypeError, ValueError):
            return
        for admin_id, value in list(self._balance_mark_overrides.items()):
            if int(value.get("sequence") or 0) <= limit:
                self._balance_mark_overrides.pop(admin_id, None)

    def _discard_balance_mark_override(self, admin_id) -> None:
        try:
            self._balance_mark_overrides.pop(int(admin_id), None)
        except (TypeError, ValueError):
            pass

    def _apply_committed_order_mark(self, index, admin, mark: str, *, sequence=None):
        override = self._balance_mark_overrides.get(int(admin.id)) if admin is not None else None
        if sequence is not None and (override is None or override.get("sequence") != sequence):
            self.balanceSnapshotRequired.emit()
            return
        if override is not None:
            override["pending"] = False
        self.balanceSnapshotRequired.emit()
        if not self.model or not index.isValid() or admin is None:
            return
        key = self._admin_key_from_admin(admin)
        if key is None:
            return
        committed_admin = copy(admin)
        committed_admin.version = int(getattr(admin, "version", 0) or 0) + 1
        committed_admin.comment = mark or ""
        committed_admin.actual_time = datetime.now() if mark else None
        if hasattr(committed_admin, "_pending_mark"):
            delattr(committed_admin, "_pending_mark")
        self.model.admin_map[key] = committed_admin
        self._emit_admin_cell_changes([key], mark_draft=False)

    def _apply_pending_cell(
        self,
        index,
        order: OrderDTO,
        admin: AdministrationDTO,
        planned_time: datetime,
        op_prefix: str,
    ) -> dict:
        if not self.model or not index.isValid():
            return {}
        key = (getattr(order, "id", None), planned_time.isoformat())
        if key[0] is None:
            return {}
        had_previous = key in self.model.admin_map
        previous_admin = copy(self.model.admin_map[key]) if had_previous else None
        pending_admin = copy(previous_admin) if previous_admin is not None else self._new_optimistic_admin(
            order,
            planned_time,
            role="single",
            previous_admin=admin,
        )
        setattr(pending_admin, "_pending_cell_action", op_prefix)
        self.model.admin_map[key] = pending_admin
        self._emit_admin_cell_changes([key])
        return {key: (had_previous, previous_admin)}

    @staticmethod
    def _is_long_order(order: OrderDTO) -> bool:
        try:
            duration = int(getattr(order, "duration_min", 0) or 0)
        except Exception:
            return False
        return duration == -1 or duration >= 61

    def _chain_keys_for_admin(self, key, admin):
        if self.model is None:
            return []
        chain_id = getattr(admin, "big_chain_id", None)
        if not chain_id:
            return [key] if key in self.model.admin_map else []
        keys = [
            item_key
            for item_key, item_admin in self.model.admin_map.items()
            if item_key[0] == key[0]
            and getattr(item_admin, "big_chain_id", None) == chain_id
            and str(getattr(item_admin, "status", "") or "") == "planned"
        ]
        keys = sorted(keys, key=lambda item: item[1])
        if key not in keys:
            return [key] if key in self.model.admin_map else []

        center = keys.index(key)
        left = center
        while left > 0:
            if datetime.fromisoformat(keys[left][1]) - datetime.fromisoformat(keys[left - 1][1]) != timedelta(hours=1):
                break
            left -= 1

        right = center
        while right + 1 < len(keys):
            if datetime.fromisoformat(keys[right + 1][1]) - datetime.fromisoformat(keys[right][1]) != timedelta(hours=1):
                break
            right += 1

        return keys[left:right + 1]

    def _optimistic_chain_slots(self, order: OrderDTO, planned_time: datetime) -> list[datetime]:
        if self.model is None:
            return []
        try:
            duration = int(getattr(order, "duration_min", 0) or 0)
        except Exception:
            duration = 0
        limit_time = planned_time.replace(hour=8, minute=0, second=0, microsecond=0)
        if planned_time.hour >= 8:
            limit_time += timedelta(days=1)
        if duration == -1:
            num_desired = int((limit_time - planned_time).total_seconds() / 3600)
        else:
            num_desired = (duration - 1) // 60 + 1
        if num_desired <= 0:
            return []

        slot_by_iso = {slot.isoformat(): slot for slot in getattr(self.model, "time_slots", [])}
        desired_slots = []
        for offset in range(num_desired):
            slot = planned_time + timedelta(hours=offset)
            if slot >= limit_time:
                break
            model_slot = slot_by_iso.get(slot.isoformat())
            if model_slot is not None:
                desired_slots.append(model_slot)
        return desired_slots

    def _new_optimistic_admin(
        self,
        order: OrderDTO,
        planned_time: datetime,
        *,
        role: str,
        chain_id: str | None = None,
        status: str = "planned",
        previous_admin: AdministrationDTO | None = None,
    ) -> AdministrationDTO:
        return AdministrationDTO(
            id=self._allocate_local_admin_id(),
            order_id=order.id,
            big_chain_id=chain_id,
            cell_role=role,
            planned_time=planned_time,
            status=status,
            is_committed=0,
            comment="",
            volume_ml=float(getattr(previous_admin, "volume_ml", 0.0) or 0.0),
        )

    def _add_optimistic_single(
        self,
        changes: _OptimisticAdminChanges,
        key,
        order: OrderDTO,
        admin: AdministrationDTO,
        planned_time: datetime,
    ) -> None:
        changes.set_admin(
            key,
            self._new_optimistic_admin(
                order,
                planned_time,
                role="single",
                previous_admin=admin,
            ),
        )

    def _add_optimistic_chain(
        self,
        changes: _OptimisticAdminChanges,
        order: OrderDTO,
        planned_time: datetime,
    ) -> None:
        desired_slots = self._optimistic_chain_slots(order, planned_time)
        available_slots = []
        for pos, slot in enumerate(desired_slots):
            item_key = (order.id, slot.isoformat())
            existing = self.model.admin_map.get(item_key)
            if (
                existing
                and str(getattr(existing, "status", "") or "") == "planned"
                and pos > 0
            ):
                break
            available_slots.append(slot)
        if not available_slots:
            return

        chain_id = (
            f"optimistic:{order.id}:{planned_time.isoformat()}"
            if len(available_slots) > 1
            else None
        )
        for pos, slot in enumerate(available_slots):
            if len(available_slots) == 1:
                role = "single"
            elif pos == 0:
                role = "start"
            elif pos == len(available_slots) - 1:
                role = "end"
            else:
                role = "body"
            item_key = (order.id, slot.isoformat())
            changes.set_admin(
                item_key,
                self._new_optimistic_admin(
                    order,
                    slot,
                    role=role,
                    chain_id=chain_id,
                    previous_admin=self.model.admin_map.get(item_key),
                ),
            )

    def _apply_middle_click_optimistic(
        self,
        changes: _OptimisticAdminChanges,
        key,
        admin: AdministrationDTO | None,
        *,
        status: str,
        role: str,
    ) -> None:
        if admin is None:
            return
        if status == "cancelled":
            changes.remove_admin(key)
            return
        if status != "planned":
            return

        chain_keys = self._chain_keys_for_admin(key, admin)
        chain_id = getattr(admin, "big_chain_id", None)
        if role == "start":
            cancelled_admin = copy(admin)
            cancelled_admin.status = "cancelled"
            cancelled_admin.cell_role = role
            changes.set_admin(key, cancelled_admin)
            for item_key in chain_keys:
                if item_key != key:
                    changes.remove_admin(item_key)
            return
        if role == "body":
            end_admin = copy(admin)
            end_admin.status = "planned"
            end_admin.cell_role = "end"
            changes.set_admin(key, end_admin)
            for item_key in chain_keys:
                if item_key[1] > key[1]:
                    changes.remove_admin(item_key)
            return
        if role == "end":
            cancelled_admin = copy(admin)
            cancelled_admin.status = "cancelled"
            cancelled_admin.cell_role = "single"
            cancelled_admin.big_chain_id = chain_id
            changes.set_admin(key, cancelled_admin)
            remaining_keys = [item_key for item_key in chain_keys if item_key != key]
            if remaining_keys:
                prev_key = max(remaining_keys, key=lambda item: item[1])
                prev_admin = copy(self.model.admin_map.get(prev_key))
                if prev_admin is not None:
                    prev_admin.cell_role = "single" if len(remaining_keys) == 1 else "end"
                    changes.set_admin(prev_key, prev_admin)
            return

        cancelled_admin = copy(admin)
        cancelled_admin.status = "cancelled"
        cancelled_admin.cell_role = "single"
        cancelled_admin.big_chain_id = chain_id
        changes.set_admin(key, cancelled_admin)

    def _trim_optimistic_chain(
        self,
        changes: _OptimisticAdminChanges,
        key,
        admin: AdministrationDTO,
        *,
        role: str,
    ) -> None:
        chain_keys = self._chain_keys_for_admin(key, admin)
        if role == "start":
            for item_key in chain_keys:
                changes.remove_admin(item_key)
            return
        if role == "body":
            end_admin = copy(admin)
            end_admin.cell_role = "end"
            changes.set_admin(key, end_admin)
            for item_key in chain_keys:
                if item_key[1] > key[1]:
                    changes.remove_admin(item_key)
            return
        if role != "end":
            return

        remaining_keys = [item_key for item_key in chain_keys if item_key != key]
        changes.remove_admin(key)
        prev_keys = [item_key for item_key in remaining_keys if item_key[1] < key[1]]
        if not prev_keys:
            return
        prev_key = max(prev_keys, key=lambda item: item[1])
        prev_admin = copy(self.model.admin_map.get(prev_key))
        if prev_admin is not None:
            prev_admin.cell_role = "single" if len(remaining_keys) == 1 else "end"
            changes.set_admin(prev_key, prev_admin)

    def _apply_primary_click_optimistic(
        self,
        changes: _OptimisticAdminChanges,
        key,
        order: OrderDTO,
        admin: AdministrationDTO | None,
        planned_time: datetime,
        *,
        status: str,
        role: str,
        is_long: bool,
    ) -> None:
        if admin is None or status in ("deleted", "cancelled"):
            if is_long:
                self._add_optimistic_chain(changes, order, planned_time)
            else:
                self._add_optimistic_single(changes, key, order, admin, planned_time)
            return
        if status != "planned":
            return
        if is_long and role in ("start", "body", "end"):
            self._trim_optimistic_chain(changes, key, admin, role=role)
        elif role == "single":
            changes.remove_admin(key)

    def _apply_optimistic_cell(
        self,
        index,
        order: OrderDTO,
        admin: AdministrationDTO,
        planned_time: datetime,
        op_prefix: str,
        *,
        perf_click_id: int | None = None,
    ) -> dict:
        """
        Мгновенная визуальная реакция на клик:
        - одиночные назначения меняем точечно;
        - длительные инфузии строим/режем локально теми же правилами, что и доменный сервис.
        """
        if not self.model or not index.isValid():
            return {}

        key = (order.id, planned_time.isoformat())
        if key[0] is None:
            self._perf_mark_click(perf_click_id, "optimistic_skip")
            return {}

        changes = _OptimisticAdminChanges(self, op_prefix)
        status = str(getattr(admin, "status", "") or "") if admin else ""
        role = str(getattr(admin, "cell_role", "") or "") if admin else ""
        is_long = self._is_long_order(order)

        if op_prefix == "orders_middle_click":
            self._apply_middle_click_optimistic(
                changes,
                key,
                admin,
                status=status,
                role=role,
            )
        elif op_prefix != "orders_right_click":
            self._apply_primary_click_optimistic(
                changes,
                key,
                order,
                admin,
                planned_time,
                status=status,
                role=role,
                is_long=is_long,
            )

        if changes.changed_keys:
            self._emit_admin_cell_changes(changes.changed_keys)
            self._mark_local_cell_draft_guard(changes.changed_keys)
            logger.info(
                "[OrdersClick] local_cell_update role=doctor admission_id=%s op=%s order_id=%s changed_cells=%s",
                self.admission_id,
                op_prefix,
                getattr(order, "id", None),
                len(set(changes.changed_keys)),
            )
            self._perf_mark_click(perf_click_id, "optimistic")
        else:
            self._perf_mark_click(perf_click_id, "optimistic_skip")
        return changes.previous_by_key

    def _enqueue_cell_write(
        self,
        description: str,
        operation,
        index,
        order: OrderDTO,
        admin: AdministrationDTO,
        planned_time: datetime,
        *,
        op_prefix: str,
        perf_click_id: int | None = None,
    ):
        if self._is_closing or not self.service:
            return
        target_admission_id = self.admission_id
        target_shift_date = self.shift_date
        target_key = self._admin_cell_write_key(getattr(order, "id", None), planned_time)
        self._admin_only_snapshot_until = time.monotonic() + self._admin_only_snapshot_window_sec
        self._begin_admin_write()
        previous_by_key = self._apply_optimistic_cell(
            index,
            order,
            admin,
            planned_time,
            op_prefix,
            perf_click_id=perf_click_id,
        )
        pending_keys = set(previous_by_key.keys()) if previous_by_key else set()
        if target_key is not None:
            pending_keys.add(target_key)
        self._mark_pending_admin_cell_writes(pending_keys)

        def on_success():
            self._clear_pending_admin_cell_writes(pending_keys)
            self._finish_admin_write()
            if not self._is_current_context(target_admission_id, target_shift_date):
                return
            self._schedule_fast_sync()
            self._schedule_state_sync()

        def on_error(exc):
            self._clear_pending_admin_cell_writes(pending_keys)
            self._finish_admin_write()
            if not self._is_current_context(target_admission_id, target_shift_date):
                return
            self._restore_admin_cells(previous_by_key)
            self.request_refresh(force=True)

        self._enqueue_write(
            description,
            operation=operation,
            on_success=on_success,
            on_error=on_error,
            block_ui=False,
            perf_click_id=perf_click_id,
        )

    def _perf_start_click(self, index, op_prefix: str) -> int | None:
        if not self._perf_enabled:
            return None

        self._perf_prune_clicks()
        self._perf_next_click_id += 1
        click_id = self._perf_next_click_id
        self._perf_clicks[click_id] = {
            "t0": time.perf_counter(),
            "row": index.row(),
            "col": index.column(),
            "op": op_prefix,
            "optimistic": None,
            "paint": None,
            "write": None,
        }
        logger.debug(
            f"[OrdersPerf] click#{click_id} start op={op_prefix} cell=({index.row()},{index.column()})"
        )
        return click_id

    def _perf_mark_click(self, click_id: int | None, stage: str, *, extra: str = ""):
        if not self._perf_enabled or click_id is None:
            return

        info = self._perf_clicks.get(click_id)
        if not info:
            return

        elapsed_ms = (time.perf_counter() - info["t0"]) * 1000.0
        if stage == "optimistic":
            info["optimistic"] = elapsed_ms
        elif stage == "paint":
            info["paint"] = elapsed_ms
        elif stage in ("write_ok", "write_error"):
            info["write"] = elapsed_ms
        logger.debug(
            f"[OrdersPerf] click#{click_id} {stage} +{elapsed_ms:.1f}ms"
            + (f" ({extra})" if extra else "")
        )
        self._perf_try_finalize(click_id)

    def _perf_try_finalize(self, click_id: int):
        if not self._perf_enabled:
            return
        info = self._perf_clicks.get(click_id)
        if not info:
            return
        if info["paint"] is None or info["write"] is None:
            return

        logger.debug(
            f"[OrdersPerf] click#{click_id} total: paint={info['paint']:.1f}ms, write={info['write']:.1f}ms, "
            f"optimistic={('%.1fms' % info['optimistic']) if info['optimistic'] is not None else 'n/a'} "
            f"op={info['op']} cell=({info['row']},{info['col']})"
        )
        self._perf_clicks.pop(click_id, None)

    def _perf_mark_first_unpainted(self):
        if not self._perf_enabled:
            return

        self._perf_prune_clicks()
        for click_id in sorted(self._perf_clicks.keys()):
            info = self._perf_clicks.get(click_id)
            if not info:
                continue
            if info["paint"] is None:
                self._perf_mark_click(click_id, "paint")
                return

    def _perf_prune_clicks(self):
        if not self._perf_enabled:
            return
        now = time.perf_counter()
        stale_ids = []
        for click_id, info in self._perf_clicks.items():
            if now - info["t0"] > 15.0:
                stale_ids.append(click_id)
        for click_id in stale_ids:
            self._perf_clicks.pop(click_id, None)
