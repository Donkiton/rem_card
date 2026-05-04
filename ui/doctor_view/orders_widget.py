from rem_card.ui.shared.custom_message_box import CustomMessageBox
import os
import sqlite3
import time
from copy import copy
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableView, 
    QHeaderView, QAbstractItemView, QFrame, QPushButton, QSizePolicy, QApplication
)
from PySide6.QtCore import QEvent, QModelIndex, QPoint, Qt, QTimer, Signal
from datetime import datetime, timedelta
from .template_dialog import TemplateSelectionDialog
from ..shared.orders_model import OrdersModel
from ..shared.orders_delegate import OrdersDelegate
from ..shared.async_call import AsyncCallThread
from .components.order_template_builder import build_orders_from_template
from rem_card.data.dto.remcard_dto import AdministrationDTO, OrderDTO, OrderStatus, OrderType
from rem_card.app.logger import logger
from rem_card.services.orders_sync_observability import record_orders_sync_event
from ..styles.theme import (BG_MAIN, BG_CARD, BG_ALT_ROW, TEXT_PRIMARY, 
                            BORDER_COLOR, BG_LIGHT)

class OrdersWidget(QWidget):
    draftStatusChanged = Signal(bool)
    administrationStatusChanged = Signal(bool)
    ordersPresenceChanged = Signal(bool)
    localBalanceChanged = Signal()
    _LOCAL_SILENT_FORCE_PREFIXES = (
        "orders_add_input:",
        "orders_left_click:",
        "orders_middle_click:",
        "orders_right_click:",
    )
    _ORDERS_CHANGE_ENTITIES = {"orders", "administrations"}

    def __init__(self, service=None, admission_id=None, shift_date=None, parent=None, defer_ui=False):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.service = remcard_service = service
        self.admission_id = admission_id
        self.shift_date = shift_date
        self._defer_ui = defer_ui
        
        self.main_layout = None
        self.model = None
        self._last_polled_change_id = 0
        self._last_polled_context_key = None
        self._fast_sync_timer = QTimer(self)
        self._fast_sync_timer.setSingleShot(True)
        self._fast_sync_timer.timeout.connect(self._run_fast_sync)
        self._silent_sync_delay_ms = max(250, int(os.getenv("REMCARD_ORDERS_SILENT_SYNC_DELAY_MS", "500")))
        self._admin_only_snapshot_window_sec = max(
            3.0,
            float(os.getenv("REMCARD_ORDERS_ADMIN_ONLY_WINDOW_SEC", "15")),
        )
        self._state_sync_timer = QTimer(self)
        self._state_sync_timer.setSingleShot(True)
        self._state_sync_timer.timeout.connect(self.check_drafts)
        self._last_poll_monotonic = 0.0
        self._min_poll_interval_sec = max(0.1, float(os.getenv("REMCARD_ORDERS_POLL_MIN_INTERVAL_SEC", "0.8")))
        self._pending_structure_change_id = 0
        self._applying_pending_structure_sync = False
        self._forced_read_only = False
        self._snapshot_worker = None
        self._snapshot_pending = False
        self._snapshot_force_pending = False
        self._snapshot_pending_source = "refresh"
        self._snapshot_pending_priority = "MEDIUM"
        self._snapshot_pending_reason = None
        self._active_request_context_key = None
        self._active_request_force = False
        self._active_request_priority = "MEDIUM"
        self._snapshot_stale = False
        self._snapshot_seq = 0
        self._last_applied_snapshot_signature = None
        self._cached_has_drafts = False
        self._cached_has_administrations = False
        self._cached_has_orders = False
        self._admin_only_snapshot_until = 0.0
        self._orders_click_seq = 0
        self._pending_admin_write_count = 0
        self._local_cell_draft_guard = False
        self._legacy_direct_snapshot_warned = False
        self._load_yesterday_worker = None
        self._change_debounce_ms = max(100, int(os.getenv("REMCARD_ORDERS_CHANGE_DEBOUNCE_MS", "120")))
        self._pending_change_context_key = None
        self._pending_change_reload = False
        self._pending_change_invalidated = False
        self._pending_change_count = 0
        self._soft_update_delay_ms = max(100, int(os.getenv("REMCARD_ORDERS_SOFT_UPDATE_DELAY_MS", "150")))
        self._soft_update_message = ""
        self._change_batch_timer = QTimer(self)
        self._change_batch_timer.setSingleShot(True)
        self._change_batch_timer.timeout.connect(self._flush_change_batch)
        self._soft_update_timer = QTimer(self)
        self._soft_update_timer.setSingleShot(True)
        self._soft_update_timer.timeout.connect(self._show_soft_update_if_needed)
        self._perf_enabled = os.getenv("REMCARD_PROFILE_ORDERS_CLICK", "0") == "1"
        self._perf_next_click_id = 0
        self._perf_clicks = {}
        self._pending_reorder_order_ids = []
        self._row_drag_state = None
        self._row_drag_ghost = None
        self._row_drag_indicator = None
        if not self._defer_ui:
            self.setup_ui()
        
        # Таймер для обновления маркера "Сейчас"
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_now_marker)
        self.timer.start(60000)

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
        self._clear_local_cell_draft_guard()

    def _clear_local_cell_draft_guard(self):
        self._local_cell_draft_guard = False

    def _mark_local_cell_draft_guard(self):
        if self.has_drafts():
            self._local_cell_draft_guard = True
            self._admin_only_snapshot_until = max(
                self._admin_only_snapshot_until,
                time.monotonic() + self._admin_only_snapshot_window_sec,
            )

    def _should_preserve_local_cell_draft(self, snapshot) -> bool:
        if not self._local_cell_draft_guard:
            return False
        if bool(snapshot.get("has_any_draft", False)):
            self._clear_local_cell_draft_guard()
            return False
        if not self.has_drafts():
            self._clear_local_cell_draft_guard()
            return False
        if time.monotonic() >= self._admin_only_snapshot_until:
            self._clear_local_cell_draft_guard()
            return False
        return True

    def _is_read_only(self) -> bool:
        """
        Проверяет, заблокирована ли карта для редактирования.
        В данной версии редактирование назначений врачом разрешено ВСЕГДА, 
        независимо от статуса пациента (в т.ч. при Исходе).
        """
        return bool(self._forced_read_only)

    def set_forced_read_only(self, enabled: bool):
        self._forced_read_only = bool(enabled)
        if hasattr(self, "input_widget") and self.input_widget is not None:
            self.input_widget.setEnabled(not self._forced_read_only)
        if hasattr(self, "table_view") and self.table_view is not None:
            self.table_view.viewport().update()

    def has_drafts(self) -> bool:
        if self.model is not None:
            return bool(self._cached_has_drafts or getattr(self.model, "has_any_draft", False))
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
        self._cached_has_drafts = True
        if self.model is not None:
            self.model.has_any_draft = True
        self.check_drafts()

    def _persist_reorder_draft(self):
        if not self._pending_reorder_order_ids or not self.admission_id or not self.shift_date:
            return
        ordered_order_ids = list(self._pending_reorder_order_ids)
        expected_revisions = self._visible_order_revision_map(ordered_order_ids)
        if not hasattr(self.service, "save_order_draft_sort"):
            return
        self._enqueue_write(
            f"orders_reorder_draft:{self.admission_id}",
            operation=lambda ids=ordered_order_ids: self.service.save_order_draft_sort(
                self.admission_id,
                self.shift_date,
                ids,
                expected_revisions=expected_revisions,
            ),
            on_success=lambda: self._schedule_state_sync(),
            on_error=lambda _exc: self.request_refresh(force=True),
            block_ui=False,
            show_error=False,
        )

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

        setattr(self.model.orders[row], "_pending_delete", True)
        if was_committed:
            self.model.has_any_draft = True
            self._cached_has_drafts = True
        elif hasattr(self.model, "_recompute_draft_flag"):
            self.model._recompute_draft_flag()
            self._cached_has_drafts = bool(self.model.has_any_draft or self._pending_reorder_order_ids)
        idx_left = self.model.index(row, 0)
        idx_right = self.model.index(row, max(0, self.model.columnCount() - 1))
        self.model.dataChanged.emit(idx_left, idx_right, [Qt.UserRole])
        if hasattr(self, "table_view"):
            self.table_view.viewport().update()
        self.check_drafts()
        self.localBalanceChanged.emit()

    def _clear_local_order_row_pending_delete(self, order_id):
        if self.model is None:
            return
        for row, item in enumerate(self.model.orders):
            if getattr(item, "id", None) == order_id:
                if hasattr(item, "_pending_delete"):
                    delattr(item, "_pending_delete")
                if hasattr(self.model, "_recompute_draft_flag"):
                    self.model._recompute_draft_flag()
                    self._cached_has_drafts = bool(self.model.has_any_draft or self._pending_reorder_order_ids)
                idx_left = self.model.index(row, 0)
                idx_right = self.model.index(row, max(0, self.model.columnCount() - 1))
                self.model.dataChanged.emit(idx_left, idx_right, [Qt.UserRole])
                if hasattr(self, "table_view"):
                    self.table_view.viewport().update()
                self.check_drafts()
                self.localBalanceChanged.emit()
                return

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

    def request_refresh(self, *, force: bool = False):
        logger.info(
            "[OrdersClick] request_refresh role=doctor admission_id=%s force=%s",
            self.admission_id,
            int(bool(force)),
        )
        self._request_snapshot(
            force=force,
            source="refresh",
            priority="HIGH",
            invalidate_reason="widget_refresh_force" if force else "widget_refresh",
        )

    def set_context(self, *, service=None, admission_id=None, shift_date=None):
        previous_context_key = self._current_context_key()
        if service is not None:
            self.service = service
        self.admission_id = admission_id
        self.shift_date = shift_date
        current_context_key = self._current_context_key()
        if previous_context_key != current_context_key:
            self._clear_pending_reorder()
            if self.model is not None:
                self.model.clear_for_context(self.admission_id, self.shift_date)
            self._reset_cached_state()
            self._reset_change_cursor()
            self._snapshot_stale = False
            self._reset_change_batch(stop_timer=True)
            self._clear_soft_update_state()

    def _get_read_coordinator(self):
        return getattr(self.service, "read_coordinator", None)

    def _resolve_read_mode(self) -> str:
        explicit_mode = str(getattr(self.service, "read_mode", "") or "").strip().lower()
        if explicit_mode in {"live", "archive"}:
            return explicit_mode
        if self._forced_read_only and getattr(self.service, "source_db_path", None):
            return "archive"
        return "live"

    def _resolve_source_db(self) -> str:
        if self._resolve_read_mode() == "archive":
            path = str(getattr(self.service, "source_db_path", "") or "").strip()
            return path or "archive"
        return "live"

    def _build_orders_context(self):
        coordinator = self._get_read_coordinator()
        if coordinator is None:
            raise RuntimeError("ReadCoordinator unavailable for OrdersWidget")
        if not self.admission_id or not self.shift_date:
            raise RuntimeError("OrdersWidget context is incomplete")
        return coordinator.make_orders_context(
            source_db=self._resolve_source_db(),
            admission_id=int(self.admission_id),
            shift_date=self.shift_date,
            role="doctor",
            mode=self._resolve_read_mode(),
            variant="full",
        )

    def _current_context_key(self):
        try:
            return self._build_orders_context().cache_key()
        except Exception:
            return None

    def _ensure_model_initialized(self):
        if self.model is None:
            self.model = OrdersModel(self.service, self.admission_id, self.shift_date)
        self._bind_model_to_table()

    def _bind_model_to_table(self):
        if self.model is None or not hasattr(self, "table_view"):
            return
        if self.table_view.model() is not self.model:
            self.table_view.setModel(self.model)
        self.table_view.verticalHeader().setDefaultSectionSize(45)
        self._apply_table_header_layout()

    def _apply_cached_snapshot_if_available(self, context=None) -> bool:
        coordinator = self._get_read_coordinator()
        if coordinator is None:
            return False
        try:
            target_context = context or self._build_orders_context()
        except Exception as exc:
            logger.warning("[OrdersWidget] Failed to build context for cache lookup: %s", exc, exc_info=True)
            return False
        snapshot = coordinator.get_cached_tab(target_context)
        if snapshot is None:
            return False
        return self._apply_snapshot_data(
            snapshot=snapshot,
            admission_id=target_context.admission_id,
            shift_date=target_context.shift_date,
            context_key=target_context.cache_key(),
        )

    def _warn_legacy_direct_snapshot_path(self):
        if self._legacy_direct_snapshot_warned:
            return
        coordinator = getattr(self.service, "read_coordinator", None)
        if coordinator is None:
            return
        self._legacy_direct_snapshot_warned = True
        try:
            context = coordinator.make_orders_context(
                source_db="live",
                admission_id=int(self.admission_id or 0),
                shift_date=self.shift_date,
                role="doctor",
                mode="archive" if bool(getattr(self, "_forced_read_only", False)) else "live",
                variant="full",
            )
            context_hash = context.hash()
        except Exception:
            context_hash = "unknown"
        logger.warning(
            "[OrdersWidget] legacy_direct_orders_snapshot_path admission_id=%s shift_date=%s context_hash=%s",
            self.admission_id,
            self.shift_date.isoformat() if self.shift_date else None,
            context_hash,
        )

    def handle_data_changes(self, payload: dict, *, tab_active: bool = True):
        if not self.service or not self.admission_id:
            return
        has_scoped_change, scoped_change_id = self._extract_scoped_orders_change_id(payload)
        changed_entities = {
            str(entity)
            for entity in (payload.get("changed_entities") or [])
            if entity is not None
        }
        if not changed_entities:
            changed_entities = {
                str(change.get("entity_name") or "")
                for change in (payload.get("changes") or [])
                if change.get("entity_name")
            }
        if self._is_local_silent_force_payload(payload, changed_entities):
            logger.info(
                "[OrdersWidget] skip local forced orders refresh admission_id=%s sources=%s entities=%s",
                self.admission_id,
                self._payload_force_sources(payload),
                sorted(changed_entities),
            )
            return
        if not payload.get("forced") and not changed_entities.intersection({"orders", "administrations"}):
            return
        if not payload.get("forced") and payload.get("changes") and not has_scoped_change:
            return
        coordinator = self._get_read_coordinator()
        if coordinator is None:
            logger.warning("[OrdersWidget] ReadCoordinator unavailable during handle_data_changes")
            return
        try:
            context = self._build_orders_context()
        except Exception:
            logger.exception("[OrdersWidget] Failed to build context for handle_data_changes")
            return
        context_key = context.cache_key()
        self._snapshot_stale = True
        if scoped_change_id > 0:
            self._last_polled_change_id = max(int(self._last_polled_change_id or 0), scoped_change_id)
            self._last_polled_context_key = context_key
        if self._pending_change_context_key not in (None, context_key):
            self._reset_change_batch(stop_timer=True)
        self._pending_change_context_key = context_key
        self._pending_change_reload = self._pending_change_reload or bool(tab_active)
        self._pending_change_count += 1
        if not self._pending_change_invalidated:
            coordinator.invalidate_tab(context, reason="change_log_orders")
            self._pending_change_invalidated = True
        self._change_batch_timer.start(self._change_debounce_ms)

    @staticmethod
    def _payload_force_sources(payload: dict) -> list[str]:
        sources: list[str] = []
        raw_many = payload.get("force_sources") or []
        if isinstance(raw_many, (list, tuple, set)):
            sources.extend(str(item) for item in raw_many if item)
        raw_one = payload.get("force_source")
        if raw_one:
            sources.append(str(raw_one))
        return list(dict.fromkeys(sources))

    def _is_local_silent_force_payload(self, payload: dict, changed_entities: set[str]) -> bool:
        if not payload.get("forced"):
            return False
        sources = self._payload_force_sources(payload)
        if not sources:
            return False
        if changed_entities and not set(changed_entities).issubset(self._ORDERS_CHANGE_ENTITIES):
            return False
        return any(
            source.startswith(prefix)
            for source in sources
            for prefix in self._LOCAL_SILENT_FORCE_PREFIXES
        )

    def _extract_scoped_orders_change_id(self, payload: dict) -> tuple[bool, int]:
        try:
            current_admission_id = int(self.admission_id or 0)
        except Exception:
            current_admission_id = 0
        if current_admission_id <= 0:
            return False, 0

        has_relevant_change = False
        max_scoped_change_id = 0
        for change in payload.get("changes") or []:
            entity_name = str(change.get("entity_name") or "")
            if entity_name not in {"orders", "administrations"}:
                continue

            admission_id = change.get("admission_id")
            if admission_id is None:
                # Unscoped order changes are relevant enough to reload, but not enough
                # to advance the admission-scoped stale-snapshot guard.
                has_relevant_change = True
                logger.warning(
                    "[OrdersSync] orders_change_without_admission_id role=doctor current_admission_id=%s "
                    "change_id=%s entity=%s action=%s payload_last_change_id=%s",
                    current_admission_id,
                    change.get("id"),
                    entity_name,
                    change.get("action"),
                    payload.get("last_change_id"),
                )
                continue
            try:
                if int(admission_id) != current_admission_id:
                    continue
            except Exception:
                continue

            has_relevant_change = True
            try:
                max_scoped_change_id = max(max_scoped_change_id, int(change.get("id") or 0))
            except Exception:
                pass
        return has_relevant_change, max_scoped_change_id

    def _queue_forced_reload_after_stale_snapshot(self, *, reason: str):
        pending_inflight = bool(self._snapshot_worker is not None)
        try:
            context_hash = self._build_orders_context().hash()
        except Exception:
            context_hash = None
        logger.warning(
            "[OrdersSync] forced_reload_after_stale_block role=doctor admission_id=%s reason=%s pending_inflight=%s context_hash=%s",
            self.admission_id,
            reason,
            int(pending_inflight),
            context_hash,
        )
        record_orders_sync_event(
            "forced_reload",
            role="doctor",
            admission_id=int(self.admission_id or 0),
            context_hash=context_hash,
            reason=reason,
            immediate=True,
        )
        if self._snapshot_worker is not None:
            self._snapshot_pending = True
            self._snapshot_force_pending = True
            self._snapshot_pending_priority = self._merge_priority(self._snapshot_pending_priority, "HIGH")
            self._snapshot_pending_source = "stale_snapshot"
            self._snapshot_pending_reason = reason
            return

        self._defer_snapshot_request(
            force=True,
            source="stale_snapshot",
            priority="HIGH",
            invalidate_reason=reason,
        )

    def _defer_snapshot_request(
        self,
        *,
        force: bool,
        source: str,
        priority: str,
        invalidate_reason: str | None = None,
    ):
        QTimer.singleShot(
            0,
            lambda: self._request_snapshot(
                force=force,
                source=source,
                priority=priority,
                invalidate_reason=invalidate_reason,
            ),
        )

    def _request_snapshot(
        self,
        *,
        force: bool = False,
        source: str = "refresh",
        priority: str = "MEDIUM",
        invalidate_reason: str | None = None,
    ):
        if not self.service or not self.admission_id:
            return

        coordinator = self._get_read_coordinator()
        if coordinator is None:
            logger.error(
                "[OrdersWidget] ReadCoordinator unavailable admission_id=%s shift_date=%s",
                self.admission_id,
                self.shift_date.isoformat() if self.shift_date else None,
            )
            return

        try:
            context = self._build_orders_context()
        except Exception as exc:
            logger.error("[OrdersWidget] Failed to build orders context: %s", exc, exc_info=True)
            return

        priority_name = self._normalize_priority(priority)
        context_key = context.cache_key()

        if self._snapshot_worker is not None:
            worker_running = self._snapshot_worker.isRunning()
            if (
                worker_running
                and self._is_request_covered_by_active(
                    context_key=context_key,
                    force=force,
                    priority=priority_name,
                )
            ):
                if hasattr(coordinator, "record_orders_ui_event"):
                    coordinator.record_orders_ui_event(
                        "duplicate_load_prevented",
                        role="doctor",
                        context_hash=context.hash(),
                    )
                logger.info(
                    "[OrdersWidget] skipped duplicate in-flight request admission_id=%s priority=%s force=%s context_hash=%s",
                    context.admission_id,
                    priority_name,
                    int(bool(force)),
                    context.hash(),
                )
                return
            self._snapshot_pending = True
            self._snapshot_force_pending = self._snapshot_force_pending or force
            self._snapshot_pending_priority = self._merge_priority(self._snapshot_pending_priority, priority)
            self._snapshot_pending_source = self._merge_source(
                self._snapshot_pending_source,
                source,
                self._snapshot_pending_priority,
                priority,
            )
            self._snapshot_pending_reason = invalidate_reason or self._snapshot_pending_reason
            return

        self._snapshot_seq += 1
        seq = self._snapshot_seq
        admission_id = context.admission_id
        shift_date = context.shift_date
        context_hash = context.hash()
        self._active_request_context_key = context_key
        self._active_request_force = bool(force)
        self._active_request_priority = priority_name
        self._schedule_soft_update_state(source=source)

        def job():
            if force:
                coordinator.invalidate_tab(
                    context,
                    reason=str(invalidate_reason or f"orders_widget_{source}"),
                )
            snapshot = coordinator.load_orders_tab(
                context,
                source=source,
                priority=priority_name,
                force_refresh=force,
            )
            return {
                "seq": seq,
                "admission_id": admission_id,
                "shift_date": shift_date,
                "context_key": context_key,
                "context_hash": context_hash,
                "priority": priority_name,
                "source": source,
                "snapshot": snapshot,
            }

        self._snapshot_worker = AsyncCallThread(job, parent=self)
        self._snapshot_worker.succeeded.connect(self._apply_snapshot)
        self._snapshot_worker.failed.connect(self._on_snapshot_failed)
        self._snapshot_worker.finished.connect(self._on_snapshot_finished)
        self._snapshot_worker.start()

    def _apply_snapshot(self, payload):
        try:
            if not isinstance(payload, dict):
                return
            if payload.get("seq") != self._snapshot_seq:
                logger.info(
                    "[OrdersWidget] discard stale snapshot seq request_seq=%s current_seq=%s context_hash=%s trace_id=%s",
                    payload.get("seq"),
                    self._snapshot_seq,
                    payload.get("context_hash"),
                    (payload.get("snapshot") or {}).get("load_trace_id"),
                )
                return
            if payload.get("admission_id") != self.admission_id:
                logger.info(
                    "[OrdersWidget] discard stale snapshot admission request_admission_id=%s current_admission_id=%s context_hash=%s trace_id=%s",
                    payload.get("admission_id"),
                    self.admission_id,
                    payload.get("context_hash"),
                    (payload.get("snapshot") or {}).get("load_trace_id"),
                )
                return
            self._apply_snapshot_data(
                snapshot=payload.get("snapshot") or {},
                admission_id=payload.get("admission_id"),
                shift_date=payload.get("shift_date"),
                context_key=payload.get("context_key"),
            )
        except Exception:
            logger.exception("[OrdersWidget] Failed to apply orders snapshot")

    def _capture_table_scroll(self):
        if not hasattr(self, "table_view"):
            return None
        try:
            return int(self.table_view.verticalScrollBar().value())
        except Exception:
            return None

    def _restore_table_scroll(self, value):
        if value is None or not hasattr(self, "table_view"):
            return

        def restore():
            try:
                bar = self.table_view.verticalScrollBar()
                bar.setValue(max(0, min(int(value), bar.maximum())))
            except Exception:
                pass

        restore()
        QTimer.singleShot(0, restore)

    def _apply_snapshot_data(self, *, snapshot, admission_id, shift_date, context_key=None) -> bool:
        if admission_id != self.admission_id:
            return False
        current_context_key = self._current_context_key()
        if context_key is not None and current_context_key is not None and context_key != current_context_key:
            coordinator = self._get_read_coordinator()
            if coordinator is not None and hasattr(coordinator, "record_orders_ui_event"):
                coordinator.record_orders_ui_event(
                    "race_reject",
                    role="doctor",
                    context_hash=snapshot.get("context_hash"),
                )
            logger.info(
                "[OrdersWidget] discard stale snapshot admission_id=%s request_context=%s current_context=%s context_hash=%s trace_id=%s",
                admission_id,
                context_key,
                current_context_key,
                snapshot.get("context_hash"),
                snapshot.get("load_trace_id"),
            )
            return False
        if context_key is None and shift_date != self.shift_date:
            logger.info(
                "[OrdersWidget] discard stale snapshot shift request_shift_date=%s current_shift_date=%s context_hash=%s trace_id=%s",
                shift_date.isoformat() if hasattr(shift_date, "isoformat") else shift_date,
                self.shift_date.isoformat() if hasattr(self.shift_date, "isoformat") else self.shift_date,
                snapshot.get("context_hash"),
                snapshot.get("load_trace_id"),
            )
            return False
        snapshot_change_id = int(snapshot.get("change_id") or 0)
        known_change_id = int(self._last_polled_change_id or 0)
        if known_change_id > 0 and self._last_polled_context_key not in (None, current_context_key):
            logger.info(
                "[OrdersWidget] reset stale cursor after context drift previous_context=%s current_context=%s known_change_id=%s",
                self._last_polled_context_key,
                current_context_key,
                known_change_id,
            )
            self._reset_change_cursor()
            known_change_id = 0
        self._snapshot_stale = snapshot_change_id < known_change_id
        if self._snapshot_stale:
            coordinator = self._get_read_coordinator()
            if coordinator is not None and hasattr(coordinator, "record_orders_ui_event"):
                coordinator.record_orders_ui_event(
                    "stale_apply_blocked",
                    role="doctor",
                    context_hash=snapshot.get("context_hash"),
                )
            record_orders_sync_event(
                "stale_blocked",
                role="doctor",
                admission_id=int(admission_id or 0),
                context_hash=snapshot.get("context_hash"),
                reason="snapshot_change_id_lt_known",
                immediate=True,
            )
            logger.warning(
                "[OrdersSync] stale_apply_blocked role=doctor admission_id=%s snapshot_change_id=%s known_change_id=%s context_hash=%s trace_id=%s",
                admission_id,
                snapshot_change_id,
                known_change_id,
                snapshot.get("context_hash"),
                snapshot.get("load_trace_id"),
            )
            self._queue_forced_reload_after_stale_snapshot(
                reason="stale_apply_blocked",
            )
            return False

        if self._should_preserve_local_cell_draft(snapshot):
            logger.info(
                "[OrdersClick] snapshot_skip_local_cell_draft_guard role=doctor admission_id=%s source=%s trace_id=%s current_has_drafts=%s snapshot_has_drafts=%s",
                admission_id,
                snapshot.get("source"),
                snapshot.get("load_trace_id"),
                int(self.has_drafts()),
                int(bool(snapshot.get("has_any_draft", False))),
            )
            self._queue_forced_reload_after_stale_snapshot(
                reason="local_cell_draft_guard",
            )
            return True

        snapshot_signature = self._snapshot_apply_signature(snapshot, context_key)
        if (
            snapshot_signature is not None
            and snapshot_signature == self._last_applied_snapshot_signature
            and not self._pending_reorder_order_ids
        ):
            logger.info(
                "[OrdersWidget] skip duplicate applied snapshot admission_id=%s context_hash=%s trace_id=%s version=%s",
                admission_id,
                snapshot.get("context_hash"),
                snapshot.get("load_trace_id"),
                snapshot.get("version"),
            )
            self._clear_soft_update_state()
            return True

        self._ensure_model_initialized()
        scroll_value = self._capture_table_scroll()
        if self._try_apply_admin_only_snapshot(
            snapshot=snapshot,
            admission_id=admission_id,
            known_change_id=known_change_id,
            snapshot_change_id=snapshot_change_id,
            current_context_key=current_context_key,
            snapshot_signature=snapshot_signature,
        ):
            return True

        logger.info(
            "[OrdersClick] snapshot_apply_reset role=doctor admission_id=%s source=%s trace_id=%s rows_before=%s orders=%s admin_rows=%s scroll=%s",
            admission_id,
            snapshot.get("source"),
            snapshot.get("load_trace_id"),
            self.model.rowCount() if self.model is not None else None,
            len(snapshot.get("orders") or []),
            len(snapshot.get("admin_rows") or []),
            scroll_value,
        )
        self.model.apply_snapshot(snapshot)
        self._apply_pending_reorder_to_model()
        self._restore_table_scroll(scroll_value)
        self._cached_has_drafts = bool(snapshot.get("has_any_draft", False)) or bool(self._pending_reorder_order_ids)
        self._cached_has_administrations = bool(snapshot.get("has_any_administrations", False))
        self._cached_has_orders = bool(snapshot.get("has_any_orders", False))
        self._last_polled_change_id = max(known_change_id, snapshot_change_id)
        if self._last_polled_change_id > 0:
            self._last_polled_context_key = current_context_key
        self._apply_table_header_layout()
        self.check_drafts()
        self.localBalanceChanged.emit()
        self._clear_soft_update_state()
        record_orders_sync_event(
            "applied",
            role="doctor",
            admission_id=int(admission_id or 0),
            context_hash=snapshot.get("context_hash"),
            reason=str(snapshot.get("source") or ""),
        )
        logger.info(
            "[OrdersWidget] applied snapshot admission_id=%s source=%s context_hash=%s trace_id=%s version=%s",
            admission_id,
            snapshot.get("source"),
            snapshot.get("context_hash"),
            snapshot.get("load_trace_id"),
            snapshot.get("version"),
        )
        self._last_applied_snapshot_signature = snapshot_signature
        return True

    def _try_apply_admin_only_snapshot(
        self,
        *,
        snapshot,
        admission_id,
        known_change_id,
        snapshot_change_id,
        current_context_key,
        snapshot_signature,
    ) -> bool:
        if self._pending_admin_write_count > 0:
            logger.info(
                "[OrdersClick] snapshot_skip_pending_local_write role=doctor admission_id=%s pending=%s source=%s trace_id=%s change_id=%s",
                admission_id,
                self._pending_admin_write_count,
                snapshot.get("source"),
                snapshot.get("load_trace_id"),
                snapshot.get("change_id"),
            )
            return True
        if self._should_preserve_local_cell_draft(snapshot):
            logger.info(
                "[OrdersClick] snapshot_admin_only_skip_local_cell_draft_guard role=doctor admission_id=%s source=%s trace_id=%s",
                admission_id,
                snapshot.get("source"),
                snapshot.get("load_trace_id"),
            )
            self._queue_forced_reload_after_stale_snapshot(
                reason="local_cell_draft_guard_admin_only",
            )
            return True
        if (
            self.model is None
            or self._pending_reorder_order_ids
            or time.monotonic() >= self._admin_only_snapshot_until
            or not hasattr(self.model, "apply_admin_rows_snapshot")
            or not self.model.apply_admin_rows_snapshot(snapshot)
        ):
            return False

        self._cached_has_drafts = bool(snapshot.get("has_any_draft", False))
        self._cached_has_administrations = bool(snapshot.get("has_any_administrations", False))
        self._cached_has_orders = bool(snapshot.get("has_any_orders", False))
        self._last_polled_change_id = max(known_change_id, snapshot_change_id)
        if self._last_polled_change_id > 0:
            self._last_polled_context_key = current_context_key
        self.check_drafts()
        if hasattr(self, "table_view"):
            self.table_view.viewport().update()
        self._clear_soft_update_state()
        record_orders_sync_event(
            "applied",
            role="doctor",
            admission_id=int(admission_id or 0),
            context_hash=snapshot.get("context_hash"),
            reason=str(snapshot.get("source") or ""),
        )
        logger.info(
            "[OrdersClick] snapshot_apply_admin_only role=doctor admission_id=%s source=%s trace_id=%s rows=%s admin_rows=%s",
            admission_id,
            snapshot.get("source"),
            snapshot.get("load_trace_id"),
            self.model.rowCount(),
            len(snapshot.get("admin_rows") or []),
        )
        self._last_applied_snapshot_signature = snapshot_signature
        return True

    def _snapshot_apply_signature(self, snapshot, context_key):
        try:
            return (
                context_key or snapshot.get("cache_key"),
                int(snapshot.get("version") or snapshot.get("change_id") or 0),
                str(snapshot.get("load_trace_id") or ""),
                id(snapshot),
            )
        except Exception:
            return None

    def _on_snapshot_failed(self, exc):
        self._clear_soft_update_state()
        logger.warning("[OrdersWidget] Orders snapshot load failed: %s", exc, exc_info=True)

    def _on_snapshot_finished(self):
        self._snapshot_worker = None
        self._active_request_context_key = None
        self._active_request_force = False
        self._active_request_priority = "MEDIUM"
        self._clear_soft_update_state()
        if self._snapshot_pending:
            force = self._snapshot_force_pending
            source = self._snapshot_pending_source
            priority = self._snapshot_pending_priority
            invalidate_reason = self._snapshot_pending_reason
            self._snapshot_pending = False
            self._snapshot_force_pending = False
            self._snapshot_pending_source = "refresh"
            self._snapshot_pending_priority = "MEDIUM"
            self._snapshot_pending_reason = None
            self._defer_snapshot_request(
                force=force,
                source=source,
                priority=priority,
                invalidate_reason=invalidate_reason,
            )

    @staticmethod
    def _normalize_priority(value: str) -> str:
        name = str(value or "MEDIUM").strip().upper()
        if name not in {"HIGH", "MEDIUM", "LOW"}:
            return "MEDIUM"
        return name

    @classmethod
    def _merge_priority(cls, current: str, incoming: str) -> str:
        weights = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
        current_name = cls._normalize_priority(current)
        incoming_name = cls._normalize_priority(incoming)
        if weights[incoming_name] > weights[current_name]:
            return incoming_name
        return current_name

    @classmethod
    def _merge_source(cls, current: str, incoming: str, current_priority: str, incoming_priority: str) -> str:
        if cls._merge_priority(current_priority, incoming_priority) == cls._normalize_priority(incoming_priority):
            return str(incoming or current or "refresh")
        return str(current or incoming or "refresh")

    def _is_request_covered_by_active(self, *, context_key, force: bool, priority: str) -> bool:
        if self._active_request_context_key != context_key:
            return False
        weights = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
        active_priority = self._normalize_priority(self._active_request_priority)
        incoming_priority = self._normalize_priority(priority)
        if self._active_request_force and not force:
            return True
        if self._active_request_force == bool(force) and weights[active_priority] >= weights[incoming_priority]:
            return True
        return False

    def _apply_table_header_layout(self):
        if not hasattr(self, "table_view"):
            return
        header = self.table_view.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.resizeSection(0, 350)

    def _reset_change_batch(self, *, stop_timer: bool):
        if stop_timer and self._change_batch_timer.isActive():
            self._change_batch_timer.stop()
        self._pending_change_context_key = None
        self._pending_change_reload = False
        self._pending_change_invalidated = False
        self._pending_change_count = 0

    def _flush_change_batch(self):
        pending_context_key = self._pending_change_context_key
        should_reload = bool(self._pending_change_reload)
        batch_count = int(self._pending_change_count or 0)
        self._reset_change_batch(stop_timer=False)
        current_context_key = self._current_context_key()
        if pending_context_key is None or current_context_key is None or pending_context_key != current_context_key:
            logger.info(
                "[OrdersWidget] discard debounced change batch pending_context=%s current_context=%s",
                pending_context_key,
                current_context_key,
            )
            return
        logger.info(
            "[OrdersWidget] flush debounced change batch count=%s reload=%s context_key=%s",
            batch_count,
            int(should_reload),
            pending_context_key,
        )
        if should_reload:
            self._request_snapshot(
                force=False,
                source="refresh",
                priority="MEDIUM",
                invalidate_reason=None,
            )

    def _should_show_soft_update(self, source: str) -> bool:
        return False

    def _schedule_soft_update_state(self, *, source: str):
        self._soft_update_timer.stop()
        self._clear_soft_update_state()

    def _show_soft_update_if_needed(self):
        return

    def _clear_soft_update_state(self):
        self._soft_update_timer.stop()
        self._soft_update_message = ""

    def _enqueue_write(
        self,
        description: str,
        operation,
        on_success=None,
        on_error=None,
        *,
        block_ui: bool = True,
        show_error: bool = True,
        perf_click_id: int | None = None,
    ):
        if not self.service:
            return

        queued_at = time.perf_counter()
        logger.info(
            "[OrdersClick] write_enqueue role=doctor admission_id=%s description=%s block_ui=%s",
            self.admission_id,
            description,
            int(bool(block_ui)),
        )
        if block_ui and hasattr(self, "frame_container"):
            self.frame_container.setEnabled(False)

        def _on_success(_):
            if block_ui and hasattr(self, "frame_container"):
                self.frame_container.setEnabled(True)
            logger.info(
                "[OrdersClick] write_success role=doctor admission_id=%s description=%s elapsed_ms=%s",
                self.admission_id,
                description,
                round((time.perf_counter() - queued_at) * 1000.0, 1),
            )
            self._perf_mark_click(perf_click_id, "write_ok")
            if on_success:
                on_success()

        def _on_error(exc):
            if block_ui and hasattr(self, "frame_container"):
                self.frame_container.setEnabled(True)
            logger.info(
                "[OrdersClick] write_error role=doctor admission_id=%s description=%s elapsed_ms=%s error=%s",
                self.admission_id,
                description,
                round((time.perf_counter() - queued_at) * 1000.0, 1),
                exc,
            )
            self._perf_mark_click(perf_click_id, "write_error", extra=str(exc))
            try:
                if on_error:
                    on_error(exc)
            finally:
                if show_error:
                    self._show_warning(f"Ошибка сохранения: {exc}")

        self.service.enqueue_write(
            description=description,
            operation=operation,
            on_success=_on_success,
            on_error=_on_error,
        )

    def _schedule_fast_sync(self):
        self._fast_sync_timer.start(self._silent_sync_delay_ms)

    def _schedule_state_sync(self, delay_ms: int = 120):
        self._state_sync_timer.start(delay_ms)

    def _begin_admin_write(self):
        self._pending_admin_write_count += 1

    def _finish_admin_write(self):
        self._pending_admin_write_count = max(0, self._pending_admin_write_count - 1)

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

    def _emit_admin_cell_changes(self, changed_keys):
        if self.model is None or not changed_keys:
            return
        changed_keys = list(dict.fromkeys(changed_keys))
        if hasattr(self.model, "_recompute_draft_flag"):
            self.model._recompute_draft_flag()
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
        self._emit_admin_cell_changes(changed_keys)

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
            and str(getattr(item_admin, "status", "") or "") != "deleted"
        ]
        return sorted(keys, key=lambda item: item[1])

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
            id=-1,
            order_id=order.id,
            big_chain_id=chain_id,
            cell_role=role,
            planned_time=planned_time,
            status=status,
            is_committed=0,
            comment="",
            volume_ml=float(getattr(previous_admin, "volume_ml", 0.0) or 0.0),
        )

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

        previous_by_key = {}
        changed_keys = []

        def remember(item_key):
            if item_key not in previous_by_key:
                had_previous = item_key in self.model.admin_map
                previous_by_key[item_key] = (
                    had_previous,
                    copy(self.model.admin_map[item_key]) if had_previous else None,
                )

        def set_admin(item_key, next_admin):
            if self.model.admin_map.get(item_key) == next_admin:
                return
            remember(item_key)
            if next_admin is not None:
                next_admin.is_committed = 0
                setattr(next_admin, "_pending_cell_action", op_prefix)
            self.model.admin_map[item_key] = next_admin
            changed_keys.append(item_key)

        def remove_admin(item_key):
            if item_key not in self.model.admin_map:
                return
            remember(item_key)
            existing = self.model.admin_map.get(item_key)
            existing_status = str(getattr(existing, "status", "") or "")
            if existing is not None and existing_status == "planned" and self._is_committed_value(
                getattr(existing, "is_committed", 0)
            ):
                tombstone = copy(existing)
                tombstone.status = "deleted"
                tombstone.is_committed = 0
                tombstone.comment = ""
                tombstone.actual_time = None
                setattr(tombstone, "_pending_cell_action", op_prefix)
                self.model.admin_map[item_key] = tombstone
            else:
                del self.model.admin_map[item_key]
            changed_keys.append(item_key)

        def add_planned_single():
            set_admin(
                key,
                self._new_optimistic_admin(
                    order,
                    planned_time,
                    role="single",
                    previous_admin=admin,
                ),
            )

        def add_planned_chain():
            desired_slots = self._optimistic_chain_slots(order, planned_time)
            available_slots = []
            for pos, slot in enumerate(desired_slots):
                item_key = (order.id, slot.isoformat())
                existing = self.model.admin_map.get(item_key)
                if existing and str(getattr(existing, "status", "") or "") != "deleted" and pos > 0:
                    break
                available_slots.append(slot)
            if not available_slots:
                return
            chain_id = None
            if len(available_slots) > 1:
                chain_id = f"optimistic:{order.id}:{planned_time.isoformat()}"
            for pos, slot in enumerate(available_slots):
                role = "single" if len(available_slots) == 1 else ("start" if pos == 0 else ("end" if pos == len(available_slots) - 1 else "body"))
                item_key = (order.id, slot.isoformat())
                set_admin(
                    item_key,
                    self._new_optimistic_admin(
                        order,
                        slot,
                        role=role,
                        chain_id=chain_id,
                        previous_admin=self.model.admin_map.get(item_key),
                    ),
                )

        status = str(getattr(admin, "status", "") or "") if admin else ""
        role = str(getattr(admin, "cell_role", "") or "") if admin else ""
        is_long = self._is_long_order(order)

        if op_prefix == "orders_right_click":
            pass
        elif op_prefix == "orders_middle_click":
            if not admin:
                pass
            elif status == "planned":
                chain_keys = self._chain_keys_for_admin(key, admin)
                chain_id = getattr(admin, "big_chain_id", None)
                if role == "start":
                    cancelled_admin = copy(admin)
                    cancelled_admin.status = "cancelled"
                    cancelled_admin.cell_role = role
                    set_admin(key, cancelled_admin)
                    for item_key in chain_keys:
                        if item_key != key:
                            remove_admin(item_key)
                elif role == "body":
                    end_admin = copy(admin)
                    end_admin.status = "planned"
                    end_admin.cell_role = "end"
                    set_admin(key, end_admin)
                    for item_key in chain_keys:
                        if item_key[1] > key[1]:
                            remove_admin(item_key)
                else:
                    cancelled_admin = copy(admin)
                    cancelled_admin.status = "cancelled"
                    cancelled_admin.cell_role = "single"
                    cancelled_admin.big_chain_id = chain_id
                    set_admin(key, cancelled_admin)
            elif status == "cancelled":
                remove_admin(key)
        else:
            if not admin or status in ("deleted", "cancelled"):
                if is_long:
                    add_planned_chain()
                else:
                    add_planned_single()
            elif status == "planned":
                if is_long and role in ("start", "body", "end"):
                    chain_keys = self._chain_keys_for_admin(key, admin)
                    if role == "start":
                        for item_key in chain_keys:
                            remove_admin(item_key)
                    elif role == "body":
                        end_admin = copy(admin)
                        end_admin.cell_role = "end"
                        set_admin(key, end_admin)
                        for item_key in chain_keys:
                            if item_key[1] > key[1]:
                                remove_admin(item_key)
                    elif role == "end":
                        remaining_keys = [item_key for item_key in chain_keys if item_key != key]
                        remove_admin(key)
                        prev_keys = [item_key for item_key in remaining_keys if item_key[1] < key[1]]
                        if prev_keys:
                            prev_key = max(prev_keys, key=lambda item: item[1])
                            prev_admin = copy(self.model.admin_map.get(prev_key))
                            if prev_admin is not None:
                                prev_admin.cell_role = "single" if len(remaining_keys) == 1 else "end"
                                set_admin(prev_key, prev_admin)
                elif role == "single":
                    remove_admin(key)

        if changed_keys:
            self._emit_admin_cell_changes(changed_keys)
            self._mark_local_cell_draft_guard()
            logger.info(
                "[OrdersClick] local_cell_update role=doctor admission_id=%s op=%s order_id=%s changed_cells=%s",
                self.admission_id,
                op_prefix,
                getattr(order, "id", None),
                len(set(changed_keys)),
            )
            self._perf_mark_click(perf_click_id, "optimistic")
        else:
            self._perf_mark_click(perf_click_id, "optimistic_skip")
        return previous_by_key

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
        target_admission_id = self.admission_id
        target_shift_date = self.shift_date
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

        def on_success():
            self._finish_admin_write()
            if not self._is_current_context(target_admission_id, target_shift_date):
                return
            self._schedule_fast_sync()
            self._schedule_state_sync()

        def on_error(exc):
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

    def finalize_card(self):
        if not self.admission_id or self._is_read_only(): return
        target_admission_id = self.admission_id
        target_shift_date = self.shift_date
        ordered_order_ids = list(self._pending_reorder_order_ids or [])
        expected_revisions = self._visible_order_revision_map()

        def after_success():
            if not self._is_current_context(target_admission_id, target_shift_date):
                return
            from rem_card.app.logger import logger
            logger.info(f"Карта назначений для ID {target_admission_id} успешно сохранена")
            self._clear_local_cell_draft_guard()
            self._admin_only_snapshot_until = 0.0
            self._clear_pending_reorder()
            self._refresh_model()

        self._enqueue_write(
            f"orders_finalize:{target_admission_id}",
            operation=lambda ids=ordered_order_ids: self.service.finalize_order_card(
                target_admission_id,
                shift_date=target_shift_date,
                ordered_order_ids=ids,
                expected_revisions=expected_revisions,
            ),
            on_success=after_success,
        )

    def clear_drafts(self):
        if not self.admission_id or self._is_read_only(): return
        target_admission_id = self.admission_id
        target_shift_date = self.shift_date
        expected_revisions = self._visible_order_revision_map()

        def after_success():
            if not self._is_current_context(target_admission_id, target_shift_date):
                return
            self._clear_local_cell_draft_guard()
            self._admin_only_snapshot_until = 0.0
            self._clear_pending_reorder()
            self._refresh_model()

        self._enqueue_write(
            f"orders_clear_drafts:{target_admission_id}",
            operation=lambda: self.service.clear_order_drafts(
                target_admission_id,
                target_shift_date,
                expected_revisions=expected_revisions,
            ),
            on_success=after_success,
        )

    def setup_data(self):
        """Обновление только данных (без пересоздания виджетов)."""
        if self.main_layout is None:
            self.setup_ui()

        if self.service and self.admission_id:
            self._ensure_model_initialized()
            if self.model is not None:
                self.model.clear_for_context(self.admission_id, self.shift_date)
            self._reset_cached_state()
            self._apply_table_header_layout()
            self._reset_change_cursor()
            if not self._apply_cached_snapshot_if_available():
                self._request_snapshot(
                    force=False,
                    source="user",
                    priority="HIGH",
                    invalidate_reason=None,
                )
        else:
            self._reset_change_cursor()
            self._reset_cached_state()
            if self.model is not None:
                self.model.clear_for_context(self.admission_id, self.shift_date)

        self.check_drafts()
        self.update_now_marker()

    def setup_ui(self):
        """Инициализация интерфейса (выполняется один раз)."""
        if self.main_layout:
            self.setup_data()
            return

        self.main_layout = QVBoxLayout(self)
        layout = self.main_layout
        # Отступ 3px сверху для унификации с другими вкладками (2в, ИВЛ и т.д.)
        layout.setContentsMargins(0, 3, 0, 5) 
        layout.setSpacing(0)
        
        self.frame_container = QFrame()
        self.frame_container.setObjectName("orders_frame_container")
        self.frame_container.setStyleSheet(f"""
            QFrame#orders_frame_container {{ 
                border: 1.5px solid {BORDER_COLOR}; 
                border-radius: 5px; 
                background-color: {BG_CARD}; 
            }}
        """)
        self.frame_layout = QVBoxLayout(self.frame_container)
        self.frame_layout.setContentsMargins(2, 2, 2, 2)
        self.frame_layout.setSpacing(5) 
        layout.addWidget(self.frame_container, 1)

        # 1. Поле поиска
        self.top_container = QFrame()
        self.top_container.setStyleSheet("background-color: transparent;")
        self.top_container.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        top_layout = QHBoxLayout(self.top_container)
        top_layout.setContentsMargins(0, 0, 0, 0)
        from .prescription_input_widget import PrescriptionInputWidget

        self.input_widget = PrescriptionInputWidget()
        self.input_widget.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.input_widget.prescription_generated.connect(self.on_prescription_input)
        top_layout.addWidget(self.input_widget, 1)
        self.frame_layout.addWidget(self.top_container, 0)

        # 2. Таблица
        self.table_clip_widget = QWidget()
        self.table_clip_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.table_clip_layout = QVBoxLayout(self.table_clip_widget)
        self.table_clip_layout.setContentsMargins(0, 0, 0, 0)
        self.frame_layout.addWidget(self.table_clip_widget, 1)

        self.table_view = QTableView()
        self.table_view.setMinimumHeight(120)
        self.table_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.delegate = OrdersDelegate()
        self.table_view.setItemDelegate(self.delegate)
        
        self.table_view.verticalHeader().setDefaultSectionSize(45)
        self.table_view.verticalHeader().setVisible(False)
        self.table_view.setSelectionMode(QAbstractItemView.NoSelection)
        self.table_view.setFocusPolicy(Qt.NoFocus)
        self.table_view.setShowGrid(False)
        self.table_view.clicked.connect(self.on_cell_clicked)
        self.table_view.viewport().installEventFilter(self)
        
        self.table_view.setStyleSheet(f"QTableView {{ border: none; background-color: {BG_CARD}; alternate-background-color: {BG_ALT_ROW}; font-size: 9pt; }} QHeaderView::section {{ background-color: {BG_LIGHT}; padding: 4px; border: 1 solid {BORDER_COLOR}; font-weight: bold; color: {TEXT_PRIMARY}; font-size: 9pt; }}")
        self.table_clip_layout.addWidget(self.table_view)

        self.bottom_footer = QWidget()
        self.bottom_footer.setFixedHeight(15)
        self.frame_layout.addWidget(self.bottom_footer)

        self.setStyleSheet(f"OrdersWidget {{ background-color: {BG_MAIN}; }} QWidget#table_clip {{ background-color: {BG_CARD}; border-top-left-radius: 5px; border-top-right-radius: 5px; }} QWidget#orders_footer_frame {{ background-color: {BG_MAIN}; border-top: 1px solid {BORDER_COLOR}; border-bottom-left-radius: 5px; border-bottom-right-radius: 5px; }} QTableView {{ border: none; background-color: {BG_CARD}; alternate-background-color: {BG_ALT_ROW}; font-size: 9pt; border-top-left-radius: 5px; border-top-right-radius: 5px; }} QHeaderView::section {{ background-color: {BG_LIGHT}; padding: 6px; border: none; border-bottom: 0.5px solid {BORDER_COLOR}; font-weight: bold; color: {TEXT_PRIMARY}; font-size: 10pt; }} QHeaderView {{ background-color: {BG_LIGHT}; border-top-left-radius: 5px; border-top-right-radius: 5px; }}")
        
        self._bind_model_to_table()
        self.set_forced_read_only(self._forced_read_only)

    def ensure_ready_for_show(self):
        """Ленивая инициализация таблицы перед первым показом вкладки."""
        if self.main_layout is None:
            self.setup_ui()

        if self.model is None:
            self.setup_data()
            return

        if self.model.admission_id != self.admission_id or self.model.shift_date != self.shift_date:
            self.setup_data()
            return

        if self.has_drafts():
            return

        if self._snapshot_stale:
            if self._apply_cached_snapshot_if_available():
                return
            self._request_snapshot(
                force=False,
                source="refresh",
                priority="HIGH",
                invalidate_reason=None,
            )
            return

        if not self.model.orders:
            if self._apply_cached_snapshot_if_available():
                return
            self._request_snapshot(
                force=False,
                source="user",
                priority="HIGH",
                invalidate_reason=None,
            )

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
        self.model.beginInsertRows(QModelIndex(), row, row)
        try:
            self.model.orders.append(copy(order))
            self.model._renumber_local_sort_order()
            if hasattr(self.model, "_recompute_draft_flag"):
                self.model._recompute_draft_flag()
        finally:
            self.model.endInsertRows()

        self._cached_has_drafts = bool(getattr(self.model, "has_any_draft", False)) or bool(self._pending_reorder_order_ids)
        self._cached_has_orders = any(
            item and item.status != OrderStatus.DELETED
            for item in self.model.orders
        )
        self._cached_has_administrations = self._model_has_administrations()
        self._admin_only_snapshot_until = time.monotonic() + self._admin_only_snapshot_window_sec
        self._apply_table_header_layout()
        self._restore_table_scroll(scroll_value)
        self.check_drafts()
        if hasattr(self, "table_view"):
            self.table_view.viewport().update()
        self._schedule_fast_sync()
        self.localBalanceChanged.emit()

    def on_prescription_input(self, text):
        if self._is_read_only(): return
        from .components.order_input_handler import OrderInputHandler

        target_admission_id = self.admission_id
        target_shift_date = self.shift_date
        new_order = OrderInputHandler.parse_input_to_dto(text, self.admission_id)
        new_order.is_committed = 0
        now = datetime.now()
        start, end = self.service.get_day_period(self.shift_date)
        new_order.created_at = now if start <= now < end else start

        self._enqueue_write(
            f"orders_add_input:{target_admission_id}",
            operation=lambda: self.service.add_order(new_order),
            on_success=lambda: (
                self._insert_local_order_after_add(new_order)
                if self._is_current_context(target_admission_id, target_shift_date)
                else None
            ),
        )
        
    def update_now_marker(self):
        if hasattr(self, 'table_view'): self.table_view.viewport().update()

    def poll_external_updates(self, force: bool = False):
        self._request_snapshot(
            force=force,
            source="refresh",
            priority="MEDIUM",
            invalidate_reason="poll_external_updates" if force else None,
        )

    def on_cell_clicked(self, index):
        self._handle_cell_action(index, "orders_left_click", self.service.apply_order_left_click)

    def _format_drag_order_text(self, order: OrderDTO) -> str:
        latin = (getattr(order, "latin", "") or "Назначение").strip()
        dose_value = getattr(order, "dose_value", 0) or 0
        dose_unit = (getattr(order, "dose_unit", "") or "").strip()
        dose = f"{dose_value:g} {dose_unit}".strip()
        if dose == "0":
            dose = ""
        return f"{latin} {dose}".strip()

    def _drag_target_row(self, pos: QPoint) -> int:
        if not self.model or not self.model.orders:
            return 0
        index = self.table_view.indexAt(pos)
        if not index.isValid():
            return 0 if pos.y() < 0 else len(self.model.orders)
        row = index.row()
        rect = self.table_view.visualRect(self.model.index(row, 0))
        if pos.y() < rect.center().y():
            return row
        return row + 1

    def _ensure_drag_indicator(self):
        if self._row_drag_indicator is not None:
            return self._row_drag_indicator
        indicator = QFrame(self.table_view.viewport())
        indicator.setObjectName("orders_row_drag_indicator")
        indicator.setFixedHeight(3)
        indicator.setStyleSheet("background-color: #2f80ed; border-radius: 1px;")
        indicator.hide()
        self._row_drag_indicator = indicator
        return indicator

    def _begin_order_row_drag(self, event):
        state = self._row_drag_state or {}
        source_row = state.get("source_row")
        if source_row is None or not self.model or source_row >= len(self.model.orders):
            return

        order = self.model.orders[source_row]
        rect = self.table_view.visualRect(self.model.index(source_row, 0))
        ghost = QLabel(self._format_drag_order_text(order), self.table_view.viewport())
        ghost.setObjectName("orders_row_drag_ghost")
        ghost.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        ghost.setFixedSize(max(120, rect.width() - 8), max(28, rect.height() - 8))
        ghost.setStyleSheet(
            "QLabel#orders_row_drag_ghost {"
            "background-color: rgba(255, 255, 255, 235);"
            "border: 1.5px solid #2f80ed;"
            "border-radius: 6px;"
            "padding-left: 8px;"
            "font-size: 9pt;"
            "color: #1f2d3d;"
            "}"
        )
        self._row_drag_ghost = ghost
        state["active"] = True
        self._row_drag_state = state
        ghost.show()
        ghost.raise_()
        self._update_order_row_drag(event.pos())

    def _update_order_row_drag(self, pos: QPoint):
        state = self._row_drag_state
        if not state or not self.model:
            return

        offset = state.get("offset", QPoint(0, 0))
        if self._row_drag_ghost is not None:
            self._row_drag_ghost.move(pos - offset)
            self._row_drag_ghost.raise_()

        target_row = self._drag_target_row(pos)
        state["target_row"] = target_row
        indicator = self._ensure_drag_indicator()
        if target_row <= 0:
            y = 0
        elif target_row >= len(self.model.orders):
            last_rect = self.table_view.visualRect(self.model.index(len(self.model.orders) - 1, 0))
            y = last_rect.bottom()
        else:
            y = self.table_view.visualRect(self.model.index(target_row, 0)).top()
        indicator.setGeometry(0, max(0, y), self.table_view.viewport().width(), 3)
        indicator.show()
        indicator.raise_()

    def _finish_order_row_drag(self, pos: QPoint):
        state = self._row_drag_state or {}
        was_active = bool(state.get("active"))
        source_row = state.get("source_row")
        target_row = state.get("target_row", self._drag_target_row(pos))
        self._cleanup_order_row_drag()

        if not was_active or source_row is None or not self.model:
            return True
        if source_row < 0 or source_row >= len(self.model.orders):
            return True

        final_row = max(0, min(int(target_row), len(self.model.orders)))
        if final_row > source_row:
            final_row -= 1
        final_row = max(0, min(final_row, len(self.model.orders) - 1))
        if self.model.move_order_row(source_row, final_row, mark_draft=True):
            self._mark_local_reorder_draft()
            self._persist_reorder_draft()
            if hasattr(self, "table_view"):
                self.table_view.viewport().update()
        return True

    def _cleanup_order_row_drag(self):
        if self._row_drag_ghost is not None:
            self._row_drag_ghost.hide()
            self._row_drag_ghost.deleteLater()
            self._row_drag_ghost = None
        if self._row_drag_indicator is not None:
            self._row_drag_indicator.hide()
        self._row_drag_state = None

    def eventFilter(self, obj, event):
        if obj is self.table_view.viewport() and event.type() == QEvent.Paint:
            self._perf_mark_first_unpainted()
        if obj is self.table_view.viewport() and event.type() == QEvent.MouseButtonPress:
            # Если карта заблокирована - игнорируем любые клики
            if self._is_read_only():
                return True 

            index = self.table_view.indexAt(event.pos())
            if index.isValid():
                if index.column() == 0 and event.button() == Qt.LeftButton:
                    rect = self.table_view.visualRect(index)
                    self._row_drag_state = {
                        "source_row": index.row(),
                        "press_pos": event.pos(),
                        "offset": event.pos() - rect.topLeft(),
                        "active": False,
                        "target_row": index.row(),
                    }
                    return True
                if index.column() == 0 and event.button() == Qt.MiddleButton:
                    row = index.row()
                    if row < 0 or row >= len(self.model.orders):
                        return True
                    order = self.model.orders[row]
                    was_committed = self._is_committed_value(getattr(order, "is_committed", 0))
                    order_id = order.id
                    expected_revision = getattr(order, "revision", None)
                    self._mark_local_order_row_deleted(row, order, was_committed=was_committed)

                    def on_delete_error(exc, oid=order_id):
                        self._clear_local_order_row_pending_delete(oid)
                        self._on_cell_write_failed(exc)

                    target_admission_id = self.admission_id
                    target_shift_date = self.shift_date
                    self._enqueue_write(
                        f"orders_soft_delete_row:{order_id}",
                        operation=lambda oid=order_id, rev=expected_revision: self.service.soft_delete_order_row(
                            oid,
                            was_committed,
                            expected_revision=rev,
                        ),
                        on_success=lambda aid=target_admission_id, sd=target_shift_date: self._refresh_model_if_current(aid, sd),
                        on_error=on_delete_error,
                    )
                    return True
                if index.column() > 0:
                    if event.button() == Qt.LeftButton and event.modifiers() == Qt.NoModifier:
                        self.on_cell_clicked(index)
                        return True
                    if event.button() == Qt.MiddleButton or (event.button() == Qt.LeftButton and event.modifiers() == Qt.AltModifier):
                        self.on_cell_middle_clicked(index)
                        return True
                    if event.button() == Qt.RightButton:
                        self.on_cell_right_clicked(index)
                        return True
        if obj is self.table_view.viewport() and event.type() == QEvent.MouseMove:
            if self._row_drag_state:
                press_pos = self._row_drag_state.get("press_pos", event.pos())
                if not self._row_drag_state.get("active"):
                    if (event.pos() - press_pos).manhattanLength() >= QApplication.startDragDistance():
                        self._begin_order_row_drag(event)
                else:
                    self._update_order_row_drag(event.pos())
                return True
        if obj is self.table_view.viewport() and event.type() == QEvent.MouseButtonRelease:
            if self._row_drag_state and event.button() == Qt.LeftButton:
                return self._finish_order_row_drag(event.pos())
        if obj is self.table_view.viewport() and event.type() in (QEvent.Leave, QEvent.Hide):
            if self._row_drag_state and not self._row_drag_state.get("active"):
                self._cleanup_order_row_drag()
        return super().eventFilter(obj, event)

    def on_cell_middle_clicked(self, index):
        self._handle_cell_action(index, "orders_middle_click", self.service.apply_order_middle_click)

    def on_cell_right_clicked(self, index):
        self._handle_cell_action(index, "orders_right_click", self.service.apply_order_right_click)

    def _next_orders_click_seq(self) -> int:
        self._orders_click_seq += 1
        return self._orders_click_seq

    def _handle_cell_action(self, index, op_prefix: str, service_action):
        if self._is_read_only():
            return
        if not index.isValid() or index.column() == 0 or not self.model:
            return
        row = index.row()
        col = index.column()
        if row < 0 or row >= len(self.model.orders):
            return
        time_slot_idx = col - 1
        if time_slot_idx < 0 or time_slot_idx >= len(self.model.time_slots):
            return
        if hasattr(self, "table_view") and self.table_view.selectionModel():
            self.table_view.selectionModel().clearSelection()
        order = self.model.orders[row]
        admin = self.model.data(index, Qt.UserRole)
        planned_time = self.model.time_slots[time_slot_idx]
        click_seq = self._next_orders_click_seq()
        logger.info(
            "[OrdersClick] click_accept role=doctor seq=%s op=%s admission_id=%s row=%s col=%s order_id=%s planned_time=%s admin_id=%s admin_status=%s admin_role=%s admin_mark=%s",
            click_seq,
            op_prefix,
            self.admission_id,
            row,
            col,
            getattr(order, "id", None),
            planned_time.isoformat(),
            getattr(admin, "id", None),
            getattr(admin, "status", None),
            getattr(admin, "cell_role", None),
            getattr(admin, "comment", None),
        )
        perf_click_id = self._perf_start_click(index, op_prefix)
        self._enqueue_cell_write(
            f"{op_prefix}:{order.id}:{planned_time.isoformat()}:seq={click_seq}",
            operation=lambda: service_action(order, admin, planned_time),
            index=index,
            order=order,
            admin=admin,
            planned_time=planned_time,
            op_prefix=op_prefix,
            perf_click_id=perf_click_id,
        )
            
    def stop_timer(self):
        if hasattr(self, 'timer') and self.timer.isActive():
            self.timer.stop()

    def start_timer(self):
        if hasattr(self, 'timer') and not self.timer.isActive():
            self.timer.start(60000)

    def _refresh_model(self):
        if self.admission_id:
            logger.debug(f"[OrdersWidget] Scheduling async refresh for ID {self.admission_id}")
        self.request_refresh(force=True)
        
    def clear_all_times(self):
        if not self.admission_id or self._is_read_only(): return
        target_admission_id = self.admission_id
        target_shift_date = self.shift_date
        self._enqueue_write(
            f"orders_clear_times:{target_admission_id}",
            operation=lambda: self.service.clear_order_times(target_admission_id, target_shift_date),
            on_success=lambda: self._refresh_model_if_current(target_admission_id, target_shift_date),
        )

    def clear_all_orders(self):
        if not self.admission_id or self._is_read_only(): return
        target_admission_id = self.admission_id
        target_shift_date = self.shift_date
        expected_revisions = self._visible_order_revision_map()

        def after_success():
            if not self._is_current_context(target_admission_id, target_shift_date):
                return
            self._clear_pending_reorder()
            self._refresh_model()

        self._enqueue_write(
            f"orders_clear_all:{target_admission_id}",
            operation=lambda: self.service.clear_order_list(
                target_admission_id,
                target_shift_date,
                expected_revisions=expected_revisions,
            ),
            on_success=after_success,
        )

    def open_template_dialog(self):
        if self._is_read_only(): return
        dlg = TemplateSelectionDialog(self)
        if dlg.exec():
            t_key = dlg.selected_template_key
            if not t_key: return
            
            from rem_card.services.prescription_engine import engine
            template = engine.templates.get(t_key)
            if not template: return
            template_type = str(template.get("template_type", "simple")).strip().lower()
            legacy_complex_mode = template_type not in ("", "simple")
            if legacy_complex_mode:
                logger.info(
                    f"[OrdersWidget] Loading legacy template '{t_key}' type='{template_type}' as simple draft list"
                )

            replace_existing = False
            expected_revisions = {}
            if self.has_orders() or self.has_drafts():
                reply = self._show_question("Лист назначений не пуст. Вы уверены, что хотите заменить текущий лист назначения?\nВсе текущие назначения будут переведены в черновики на удаление.")
                if reply != CustomMessageBox.Yes: return
                replace_existing = True
                expected_revisions = self._visible_order_revision_map()
                
            def operation():
                now = datetime.now()
                start, end = self.service.get_day_period(self.shift_date)
                base_time = now if start <= now < end else start

                orders_to_add = build_orders_from_template(
                    template=template,
                    engine=engine,
                    admission_id=self.admission_id,
                    base_time=base_time,
                )

                if replace_existing:
                    self.service.clear_order_list(
                        self.admission_id,
                        self.shift_date,
                        expected_revisions=expected_revisions,
                    )
                self.service.add_orders_batch(orders_to_add)

            def after_success():
                self._clear_pending_reorder()
                self._refresh_model()
                if legacy_complex_mode:
                    self._show_info(
                        f"Шаблон '{template.get('name', t_key)}' загружен в простом режиме "
                        f"(без автозаполнения временных ячеек)."
                    )
                    return
                self._show_info(f"Шаблон '{template.get('name', t_key)}' успешно загружен как черновик.")

            self._enqueue_write(
                f"orders_load_template:{self.admission_id}",
                operation=operation,
                on_success=after_success,
            )

    def load_yesterday_orders(self):
        if not self.admission_id or not self.service or self._is_read_only(): return
        
        reply = self._show_question("Вы уверены, что хотите загрузить вчерашние назначения?")
        if reply != CustomMessageBox.Yes: return

        if self.has_drafts():
            if self._show_question("На листе есть несохраненные изменения. Они будут потеряны. Продолжить?") == CustomMessageBox.No: return

        if self._load_yesterday_worker and self._load_yesterday_worker.isRunning():
            return

        admission_id = self.admission_id
        shift_date = self.shift_date

        def job():
            orders, found_date = self.service.find_recent_orders_source(
                admission_id,
                shift_date,
                max_days_back=3,
            )
            return {
                "admission_id": admission_id,
                "shift_date": shift_date,
                "orders": orders,
                "found_date": found_date,
            }

        self._load_yesterday_worker = AsyncCallThread(job, parent=self)
        self._load_yesterday_worker.succeeded.connect(self._on_load_yesterday_ready)
        self._load_yesterday_worker.failed.connect(self._on_load_yesterday_failed)
        self._load_yesterday_worker.finished.connect(self._on_load_yesterday_finished)
        self._load_yesterday_worker.start()

    def _on_load_yesterday_ready(self, payload):
        if not isinstance(payload, dict):
            return
        if payload.get("admission_id") != self.admission_id or payload.get("shift_date") != self.shift_date:
            return

        yesterday_orders = payload.get("orders") or []
        found_date = payload.get("found_date")
        if not yesterday_orders or not found_date:
            self._show_info("За последние 3 дня назначений не найдено.")
            return

        if found_date.date() < (self.shift_date - timedelta(days=1)).date():
            if self._show_question(f"Найдены назначения за {found_date.strftime('%d.%m.%Y')}. Загрузить?") == CustomMessageBox.No:
                return

        expected_revisions = self._visible_order_revision_map()
        target_admission_id = self.admission_id
        target_shift_date = self.shift_date

        def after_success():
            if not self._is_current_context(target_admission_id, target_shift_date):
                return
            self._clear_pending_reorder()
            self._refresh_model()

        self._enqueue_write(
            f"orders_load_yesterday:{target_admission_id}",
            operation=lambda: self.service.replace_orders_from_date(
                admission_id=target_admission_id,
                target_shift_date=target_shift_date,
                source_shift_date=found_date,
                source_orders=yesterday_orders,
                expected_revisions=expected_revisions,
            ),
            on_success=after_success,
        )

    def _on_load_yesterday_failed(self, exc):
        self._show_warning(f"Не удалось найти назначения за предыдущие дни: {exc}")

    def _on_load_yesterday_finished(self):
        self._load_yesterday_worker = None

    def _show_question(self, text):
        return CustomMessageBox.question(self, "Подтверждение", text)

    def _show_info(self, text):
        CustomMessageBox.information(self, "Информация", text)

    def _show_warning(self, text):
        CustomMessageBox.warning(self, "Предупреждение", text)
