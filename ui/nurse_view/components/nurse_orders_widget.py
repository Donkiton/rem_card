from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTableView,
    QHeaderView, QAbstractItemView, QFrame, QSizePolicy
)
from PySide6.QtCore import Qt, QTimer, Signal
from datetime import datetime, timedelta
from copy import copy
import os
import sqlite3
import time
from ...shared.orders_model import OrdersModel
from ...shared.orders_delegate import OrdersDelegate
from ...shared.async_call import AsyncCallThread
from rem_card.data.dto.remcard_dto import OrderStatus
from rem_card.app.logger import logger
from ...styles.theme import (BG_MAIN, BG_CARD, BG_ALT_ROW, TEXT_PRIMARY,
                            BORDER_COLOR, BG_LIGHT)
from PySide6.QtGui import QColor
from ...shared.custom_message_box import CustomMessageBox
from rem_card.services.orders_sync_observability import record_orders_sync_event
from rem_card.services.order_domain_service import (
    NURSE_MARK_EXECUTED, NURSE_MARK_NOT_EXECUTED
)

class NurseOrdersDelegate(OrdersDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.highlighted_row = None
        self.highlight_color = QColor(100, 180, 255, 60) # Полупрозрачный голубой цвет

    def paint(self, painter, option, index):
        # Сначала рисуем оригинальное содержимое
        super().paint(painter, option, index)
        
        # Затем, если строка выделена, накладываем полупрозрачный фон (быстрая проверка по row)
        if self.highlighted_row is not None and index.row() == self.highlighted_row:
            painter.save()
            painter.fillRect(option.rect, self.highlight_color)
            painter.restore()

class NurseOrdersWidget(QWidget):
    """Лист назначений для медсестры."""
    draftStatusChanged = Signal(bool)
    administrationStatusChanged = Signal(bool)
    ordersPresenceChanged = Signal(bool)
    orderMarked = Signal()
    localBalanceChanged = Signal()
    _LOCAL_SILENT_FORCE_PREFIXES = ("nurse_order_mark:",)
    _ORDERS_CHANGE_ENTITIES = {"orders", "administrations"}

    def __init__(self, service=None, admission_id=None, shift_date=None, parent=None, defer_ui=False):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.service = service
        self.admission_id = admission_id
        self.shift_date = shift_date
        self._defer_ui = defer_ui
        
        self.main_layout = None
        self.model = None
        self.highlighted_order_id = None
        self._last_polled_change_id = 0
        self._last_polled_context_key = None
        self._last_poll_monotonic = 0.0
        self._min_poll_interval_sec = max(0.1, float(os.getenv("REMCARD_ORDERS_POLL_MIN_INTERVAL_SEC", "0.8")))
        self._silent_sync_delay_ms = max(250, int(os.getenv("REMCARD_ORDERS_SILENT_SYNC_DELAY_MS", "500")))
        self._is_closing = False
        self._admin_only_snapshot_window_sec = max(
            3.0,
            float(os.getenv("REMCARD_ORDERS_ADMIN_ONLY_WINDOW_SEC", "15")),
        )
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
        self._cached_has_administrations = False
        self._cached_has_orders = False
        self._admin_only_snapshot_until = 0.0
        self._orders_click_seq = 0
        self._pending_admin_write_count = 0
        self._legacy_direct_snapshot_warned = False
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
        self._silent_sync_timer = QTimer(self)
        self._silent_sync_timer.setSingleShot(True)
        self._silent_sync_timer.timeout.connect(self._run_silent_sync)
        if not self._defer_ui:
            self.setup_ui()
        
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_now_marker)
        self.timer.start(60000)

    def _is_read_only(self) -> bool:
        # Для медсестры сейчас всё Read-Only
        return True

    def _reset_change_cursor(self):
        self._last_poll_monotonic = 0.0
        self._last_polled_change_id = 0
        self._last_polled_context_key = None

    def _reset_cached_state(self):
        self._cached_has_administrations = False
        self._cached_has_orders = False
        self._last_applied_snapshot_signature = None

    def _reset_pending_snapshot_request(self):
        self._snapshot_pending = False
        self._snapshot_force_pending = False
        self._snapshot_pending_source = "refresh"
        self._snapshot_pending_priority = "MEDIUM"
        self._snapshot_pending_reason = None

    def _disconnect_snapshot_worker(self, worker):
        if worker is None:
            return
        for signal, slot in (
            (worker.succeeded, self._apply_snapshot),
            (worker.failed, self._on_snapshot_failed),
            (worker.finished, self._on_snapshot_finished),
        ):
            try:
                signal.disconnect(slot)
            except Exception:
                pass

    def shutdown(self, timeout_ms: int = 1200):
        self._is_closing = True
        self._reset_pending_snapshot_request()
        for timer_name in (
            "_silent_sync_timer",
            "_change_batch_timer",
            "_soft_update_timer",
            "timer",
        ):
            timer = getattr(self, timer_name, None)
            if timer is not None:
                timer.stop()

        worker = self._snapshot_worker
        self._snapshot_worker = None
        self._active_request_context_key = None
        self._active_request_force = False
        self._active_request_priority = "MEDIUM"
        self._disconnect_snapshot_worker(worker)
        if worker is not None and worker.isRunning():
            worker.quit()
            worker.wait(timeout_ms)

    def has_drafts(self) -> bool:
        return False

    def has_administrations(self) -> bool:
        if self.model is None:
            return False
        return bool(self._cached_has_administrations)

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

    def check_drafts(self):
        self.draftStatusChanged.emit(self.has_drafts())
        self.administrationStatusChanged.emit(self.has_administrations())
        self.ordersPresenceChanged.emit(self.has_orders())

    def request_refresh(self, *, force: bool = False):
        logger.info(
            "[OrdersClick] request_refresh role=nurse admission_id=%s force=%s",
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
            if self.model is not None:
                self.model.clear_for_context(self.admission_id, self.shift_date)
            self._reset_cached_state()
            self._reset_change_cursor()
            self.highlighted_order_id = None
            self._snapshot_stale = False
            self._reset_change_batch(stop_timer=True)
            self._clear_soft_update_state()

    def _get_read_coordinator(self):
        return getattr(self.service, "read_coordinator", None)

    def _resolve_read_mode(self) -> str:
        explicit_mode = str(getattr(self.service, "read_mode", "") or "").strip().lower()
        if explicit_mode in {"live", "archive"}:
            return explicit_mode
        return "live"

    def _resolve_source_db(self) -> str:
        if self._resolve_read_mode() == "archive":
            path = str(getattr(self.service, "source_db_path", "") or "").strip()
            return path or "archive"
        return "live"

    def _build_orders_context(self):
        coordinator = self._get_read_coordinator()
        if coordinator is None:
            raise RuntimeError("ReadCoordinator unavailable for NurseOrdersWidget")
        if not self.admission_id or not self.shift_date:
            raise RuntimeError("NurseOrdersWidget context is incomplete")
        return coordinator.make_orders_context(
            source_db=self._resolve_source_db(),
            admission_id=int(self.admission_id),
            shift_date=self.shift_date,
            role="nurse",
            mode=self._resolve_read_mode(),
            variant="committed",
        )

    def _current_context_key(self):
        try:
            return self._build_orders_context().cache_key()
        except Exception:
            return None

    def _ensure_model_initialized(self):
        if self.model is None:
            self.model = OrdersModel(self.service, self.admission_id, self.shift_date, only_committed=True)
            if hasattr(self, "table_view"):
                self.table_view.setModel(self.model)

    def _apply_cached_snapshot_if_available(self, context=None) -> bool:
        coordinator = self._get_read_coordinator()
        if coordinator is None:
            return False
        try:
            target_context = context or self._build_orders_context()
        except Exception as exc:
            logger.warning("[NurseOrdersWidget] Failed to build context for cache lookup: %s", exc, exc_info=True)
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
                role="nurse",
                mode="live",
                variant="committed",
            )
            context_hash = context.hash()
        except Exception:
            context_hash = "unknown"
        logger.warning(
            "[NurseOrdersWidget] legacy_direct_orders_snapshot_path admission_id=%s shift_date=%s context_hash=%s",
            self.admission_id,
            self.shift_date.isoformat() if self.shift_date else None,
            context_hash,
        )

    def handle_data_changes(self, payload: dict, *, tab_active: bool = True):
        if self._is_closing or not self.service or not self.admission_id:
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
                "[NurseOrdersWidget] skip local forced orders refresh admission_id=%s sources=%s entities=%s",
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
            logger.warning("[NurseOrdersWidget] ReadCoordinator unavailable during handle_data_changes")
            return
        try:
            context = self._build_orders_context()
        except Exception:
            logger.exception("[NurseOrdersWidget] Failed to build context for handle_data_changes")
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
                    "[OrdersSync] orders_change_without_admission_id role=nurse current_admission_id=%s "
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
        if self._is_closing:
            return
        pending_inflight = bool(self._snapshot_worker is not None)
        try:
            context_hash = self._build_orders_context().hash()
        except Exception:
            context_hash = None
        logger.warning(
            "[OrdersSync] forced_reload_after_stale_block role=nurse admission_id=%s reason=%s pending_inflight=%s context_hash=%s",
            self.admission_id,
            reason,
            int(pending_inflight),
            context_hash,
        )
        record_orders_sync_event(
            "forced_reload",
            role="nurse",
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
        if self._is_closing:
            return
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
        if self._is_closing or not self.service or not self.admission_id:
            return

        coordinator = self._get_read_coordinator()
        if coordinator is None:
            logger.error(
                "[NurseOrdersWidget] ReadCoordinator unavailable admission_id=%s shift_date=%s",
                self.admission_id,
                self.shift_date.isoformat() if self.shift_date else None,
            )
            return

        try:
            context = self._build_orders_context()
        except Exception as exc:
            logger.error("[NurseOrdersWidget] Failed to build orders context: %s", exc, exc_info=True)
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
                        role="nurse",
                        context_hash=context.hash(),
                    )
                logger.info(
                    "[NurseOrdersWidget] skipped duplicate in-flight request admission_id=%s priority=%s force=%s context_hash=%s",
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
                    reason=str(invalidate_reason or f"nurse_orders_{source}"),
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

        self._snapshot_worker = AsyncCallThread(job)
        self._snapshot_worker.succeeded.connect(self._apply_snapshot)
        self._snapshot_worker.failed.connect(self._on_snapshot_failed)
        self._snapshot_worker.finished.connect(self._on_snapshot_finished)
        self._snapshot_worker.start()

    def _apply_snapshot(self, payload):
        if self._is_closing:
            return
        try:
            if not isinstance(payload, dict):
                return
            if payload.get("seq") != self._snapshot_seq:
                logger.info(
                    "[NurseOrdersWidget] discard stale snapshot seq request_seq=%s current_seq=%s context_hash=%s trace_id=%s",
                    payload.get("seq"),
                    self._snapshot_seq,
                    payload.get("context_hash"),
                    (payload.get("snapshot") or {}).get("load_trace_id"),
                )
                return
            if payload.get("admission_id") != self.admission_id:
                logger.info(
                    "[NurseOrdersWidget] discard stale snapshot admission request_admission_id=%s current_admission_id=%s context_hash=%s trace_id=%s",
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
            logger.exception("[NurseOrdersWidget] Failed to apply orders snapshot")

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
        if self._is_closing:
            return False
        if admission_id != self.admission_id:
            return False
        current_context_key = self._current_context_key()
        if context_key is not None and current_context_key is not None and context_key != current_context_key:
            coordinator = self._get_read_coordinator()
            if coordinator is not None and hasattr(coordinator, "record_orders_ui_event"):
                coordinator.record_orders_ui_event(
                    "race_reject",
                    role="nurse",
                    context_hash=snapshot.get("context_hash"),
                )
            logger.info(
                "[NurseOrdersWidget] discard stale snapshot admission_id=%s request_context=%s current_context=%s context_hash=%s trace_id=%s",
                admission_id,
                context_key,
                current_context_key,
                snapshot.get("context_hash"),
                snapshot.get("load_trace_id"),
            )
            return False
        if context_key is None and shift_date != self.shift_date:
            logger.info(
                "[NurseOrdersWidget] discard stale snapshot shift request_shift_date=%s current_shift_date=%s context_hash=%s trace_id=%s",
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
                "[NurseOrdersWidget] reset stale cursor after context drift previous_context=%s current_context=%s known_change_id=%s",
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
                    role="nurse",
                    context_hash=snapshot.get("context_hash"),
                )
            record_orders_sync_event(
                "stale_blocked",
                role="nurse",
                admission_id=int(admission_id or 0),
                context_hash=snapshot.get("context_hash"),
                reason="snapshot_change_id_lt_known",
                immediate=True,
            )
            logger.warning(
                "[OrdersSync] stale_apply_blocked role=nurse admission_id=%s snapshot_change_id=%s known_change_id=%s context_hash=%s trace_id=%s",
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

        snapshot_signature = self._snapshot_apply_signature(snapshot, context_key)
        if snapshot_signature is not None and snapshot_signature == self._last_applied_snapshot_signature:
            logger.info(
                "[NurseOrdersWidget] skip duplicate applied snapshot admission_id=%s context_hash=%s trace_id=%s version=%s",
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
            "[OrdersClick] snapshot_apply_reset role=nurse admission_id=%s source=%s trace_id=%s rows_before=%s orders=%s admin_rows=%s scroll=%s",
            admission_id,
            snapshot.get("source"),
            snapshot.get("load_trace_id"),
            self.model.rowCount() if self.model is not None else None,
            len(snapshot.get("orders") or []),
            len(snapshot.get("admin_rows") or []),
            scroll_value,
        )
        self.model.apply_snapshot(snapshot)
        self._restore_table_scroll(scroll_value)
        self._cached_has_administrations = bool(snapshot.get("has_any_administrations", False))
        self._cached_has_orders = bool(snapshot.get("has_any_orders", False))
        self._last_polled_change_id = max(known_change_id, snapshot_change_id)
        if self._last_polled_change_id > 0:
            self._last_polled_context_key = current_context_key
        self._apply_table_header_layout()
        self._restore_highlight()
        self.check_drafts()
        if hasattr(self, "table_view"):
            self.table_view.viewport().update()
        self._clear_soft_update_state()
        record_orders_sync_event(
            "applied",
            role="nurse",
            admission_id=int(admission_id or 0),
            context_hash=snapshot.get("context_hash"),
            reason=str(snapshot.get("source") or ""),
        )
        logger.info(
            "[NurseOrdersWidget] applied snapshot admission_id=%s source=%s context_hash=%s trace_id=%s version=%s",
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
                "[OrdersClick] snapshot_skip_pending_local_write role=nurse admission_id=%s pending=%s source=%s trace_id=%s change_id=%s",
                admission_id,
                self._pending_admin_write_count,
                snapshot.get("source"),
                snapshot.get("load_trace_id"),
                snapshot.get("change_id"),
            )
            return True
        if (
            self.model is None
            or time.monotonic() >= self._admin_only_snapshot_until
            or not hasattr(self.model, "apply_admin_rows_snapshot")
            or not self.model.apply_admin_rows_snapshot(snapshot)
        ):
            return False

        self._cached_has_administrations = bool(snapshot.get("has_any_administrations", False))
        self._cached_has_orders = bool(snapshot.get("has_any_orders", False))
        self._last_polled_change_id = max(known_change_id, snapshot_change_id)
        if self._last_polled_change_id > 0:
            self._last_polled_context_key = current_context_key
        self._restore_highlight()
        self.check_drafts()
        if hasattr(self, "table_view"):
            self.table_view.viewport().update()
        self._clear_soft_update_state()
        record_orders_sync_event(
            "applied",
            role="nurse",
            admission_id=int(admission_id or 0),
            context_hash=snapshot.get("context_hash"),
            reason=str(snapshot.get("source") or ""),
        )
        logger.info(
            "[OrdersClick] snapshot_apply_admin_only role=nurse admission_id=%s source=%s trace_id=%s rows=%s admin_rows=%s",
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
        if self._is_closing:
            return
        self._clear_soft_update_state()
        logger.warning("[NurseOrdersWidget] Orders snapshot load failed: %s", exc, exc_info=True)

    def _on_snapshot_finished(self):
        worker = self.sender()
        if worker is not None and self._snapshot_worker is not worker:
            return
        self._snapshot_worker = None
        self._active_request_context_key = None
        self._active_request_force = False
        self._active_request_priority = "MEDIUM"
        self._clear_soft_update_state()
        if self._is_closing:
            self._reset_pending_snapshot_request()
            return
        if self._snapshot_pending:
            force = self._snapshot_force_pending
            source = self._snapshot_pending_source
            priority = self._snapshot_pending_priority
            invalidate_reason = self._snapshot_pending_reason
            self._reset_pending_snapshot_request()
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
        if self._is_closing:
            self._reset_change_batch(stop_timer=False)
            return
        pending_context_key = self._pending_change_context_key
        should_reload = bool(self._pending_change_reload)
        batch_count = int(self._pending_change_count or 0)
        self._reset_change_batch(stop_timer=False)
        current_context_key = self._current_context_key()
        if pending_context_key is None or current_context_key is None or pending_context_key != current_context_key:
            logger.info(
                "[NurseOrdersWidget] discard debounced change batch pending_context=%s current_context=%s",
                pending_context_key,
                current_context_key,
            )
            return
        logger.info(
            "[NurseOrdersWidget] flush debounced change batch count=%s reload=%s context_key=%s",
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

    def _enqueue_write(self, description: str, operation, on_success=None, on_error=None, *, block_ui: bool = True):
        if self._is_closing or not self.service:
            return

        queued_at = time.perf_counter()
        logger.info(
            "[OrdersClick] write_enqueue role=nurse admission_id=%s description=%s block_ui=%s",
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
                "[OrdersClick] write_success role=nurse admission_id=%s description=%s elapsed_ms=%s",
                self.admission_id,
                description,
                round((time.perf_counter() - queued_at) * 1000.0, 1),
            )
            if on_success:
                on_success()

        def _on_error(exc):
            if block_ui and hasattr(self, "frame_container"):
                self.frame_container.setEnabled(True)
            logger.info(
                "[OrdersClick] write_error role=nurse admission_id=%s description=%s elapsed_ms=%s error=%s",
                self.admission_id,
                description,
                round((time.perf_counter() - queued_at) * 1000.0, 1),
                exc,
            )
            try:
                if on_error:
                    on_error(exc)
            finally:
                self._show_warning(f"Ошибка сохранения: {exc}")

        self.service.enqueue_write(
            description=description,
            operation=operation,
            on_success=_on_success,
            on_error=_on_error,
        )

    def setup_data(self):
        if self.main_layout is None:
            self.setup_ui()

        if self.service and self.admission_id:
            self._ensure_model_initialized()
            if self.model is not None:
                self.model.clear_for_context(self.admission_id, self.shift_date)
            self._reset_change_cursor()
            self._reset_cached_state()
            self._restore_highlight()
            self._apply_table_header_layout()
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
        if self.main_layout:
            self.setup_data()
            return

        self.main_layout = QVBoxLayout(self)
        layout = self.main_layout
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

        self.table_clip_widget = QWidget()
        self.table_clip_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.table_clip_layout = QVBoxLayout(self.table_clip_widget)
        self.table_clip_layout.setContentsMargins(0, 0, 0, 0)
        self.frame_layout.addWidget(self.table_clip_widget, 1)

        self.table_view = QTableView()
        self.table_view.setMinimumHeight(120)
        self.table_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.delegate = NurseOrdersDelegate()
        self.table_view.setItemDelegate(self.delegate)
        self.table_view.pressed.connect(self._on_table_clicked)
        self.table_view.viewport().installEventFilter(self)
        
        self.table_view.verticalHeader().setDefaultSectionSize(45)
        self.table_view.verticalHeader().setVisible(False)
        self.table_view.setSelectionMode(QAbstractItemView.NoSelection)
        self.table_view.setFocusPolicy(Qt.NoFocus)
        self.table_view.setShowGrid(False)
        
        self.table_view.setStyleSheet(f"QTableView {{ border: none; background-color: {BG_CARD}; alternate-background-color: {BG_ALT_ROW}; font-size: 9pt; }} QHeaderView::section {{ background-color: {BG_LIGHT}; padding: 4px; border: 1 solid {BORDER_COLOR}; font-weight: bold; color: {TEXT_PRIMARY}; font-size: 9pt; }}")
        self.table_clip_layout.addWidget(self.table_view)

        self.bottom_footer = QWidget()
        self.bottom_footer.setFixedHeight(15)
        self.frame_layout.addWidget(self.bottom_footer)

        self.setStyleSheet(f"NurseOrdersWidget {{ background-color: {BG_MAIN}; }} QWidget#table_clip {{ background-color: {BG_CARD}; border-top-left-radius: 5px; border-top-right-radius: 5px; }} QWidget#orders_footer_frame {{ background-color: {BG_MAIN}; border-top: 1px solid {BORDER_COLOR}; border-bottom-left-radius: 5px; border-bottom-right-radius: 5px; }} QTableView {{ border: none; background-color: {BG_CARD}; alternate-background-color: {BG_ALT_ROW}; font-size: 9pt; border-top-left-radius: 5px; border-top-right-radius: 5px; }} QHeaderView::section {{ background-color: {BG_LIGHT}; padding: 6px; border: none; border-bottom: 0.5px solid {BORDER_COLOR}; font-weight: bold; color: {TEXT_PRIMARY}; font-size: 10pt; }} QHeaderView {{ background-color: {BG_LIGHT}; border-top-left-radius: 5px; border-top-right-radius: 5px; }}")

    def update_now_marker(self):
        if hasattr(self, 'table_view'): self.table_view.viewport().update()

    def ensure_ready_for_show(self):
        """
        Подготавливает таблицу к показу без ожидания следующего тика таймера.
        Вызывается при открытии вкладки "Назначения".
        """
        if not self.admission_id or not self.service:
            return

        if self.model is None:
            self.setup_data()
            return

        context_changed = (
            self.model.admission_id != self.admission_id
            or self.model.shift_date != self.shift_date
        )
        if context_changed:
            self.setup_data()
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

    def poll_external_updates(self, force: bool = False):
        self._request_snapshot(
            force=force,
            source="refresh",
            priority="MEDIUM",
            invalidate_reason="poll_external_updates" if force else None,
        )

    def stop_timer(self):
        if hasattr(self, 'timer') and self.timer.isActive():
            self.timer.stop()

    def start_timer(self):
        if hasattr(self, 'timer') and not self.timer.isActive():
            self.timer.start(60000)

    def _refresh_model(self):
        self.request_refresh(force=True)

    def _restore_highlight(self):
        if not hasattr(self, 'delegate') or not self.model:
            return
            
        if self.highlighted_order_id is None:
            self.delegate.highlighted_row = None
            return
            
        # Ищем строку по ID назначения
        orders = getattr(self.model, 'orders', [])
        for i, order in enumerate(orders):
            if order.id == self.highlighted_order_id:
                self.delegate.highlighted_row = i
                return
                
        # Если не нашли (назначение удалено)
        self.highlighted_order_id = None
        self.delegate.highlighted_row = None

    def _next_orders_click_seq(self) -> int:
        self._orders_click_seq += 1
        return self._orders_click_seq

    def _begin_admin_write(self):
        self._pending_admin_write_count += 1

    def _finish_admin_write(self):
        self._pending_admin_write_count = max(0, self._pending_admin_write_count - 1)

    def _schedule_silent_sync(self):
        self._silent_sync_timer.start(self._silent_sync_delay_ms)

    def _run_silent_sync(self):
        logger.info(
            "[OrdersClick] silent_sync_start role=nurse admission_id=%s pending=%s",
            self.admission_id,
            self._pending_admin_write_count,
        )
        if self._pending_admin_write_count > 0:
            self._schedule_silent_sync()
            return
        self._request_snapshot(
            force=False,
            source="local_silent_sync",
            priority="LOW",
            invalidate_reason=None,
        )

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
        if planned_time is None:
            return None
        order_id = getattr(admin, "order_id", None)
        if order_id is None:
            return None
        return (order_id, planned_time.isoformat())

    def _restore_admin_cell(self, index, key, previous_admin):
        if self.model is None or key is None or not index.isValid():
            return
        if previous_admin is not None:
            self.model.admin_map[key] = copy(previous_admin)
        else:
            self.model.admin_map.pop(key, None)
        self._cached_has_administrations = self._model_has_administrations()
        self.model.dataChanged.emit(index, index, [Qt.UserRole])
        self.check_drafts()
        if hasattr(self, "table_view"):
            self.table_view.viewport().update()
        self.localBalanceChanged.emit()

    def _apply_optimistic_nurse_mark(self, index, admin, mark: str):
        if not self.model or not index.isValid() or admin is None:
            return
        planned_time = getattr(admin, "planned_time", None)
        if isinstance(planned_time, str):
            try:
                planned_time = datetime.fromisoformat(planned_time)
            except Exception:
                planned_time = None
        if planned_time is None:
            return
        key = (getattr(admin, "order_id", None), planned_time.isoformat())
        if key[0] is None:
            return
        optimistic_admin = copy(admin)
        optimistic_admin.comment = mark or ""
        optimistic_admin.actual_time = datetime.now() if mark else None
        self.model.admin_map[key] = optimistic_admin
        self.model.dataChanged.emit(index, index, [Qt.UserRole])
        if hasattr(self, "table_view"):
            self.table_view.viewport().update()

    def _apply_pending_nurse_mark(self, index, admin, mark: str):
        if not self.model or not index.isValid() or admin is None:
            return
        key = self._admin_key_from_admin(admin)
        if key is None:
            return
        pending_admin = copy(admin)
        setattr(pending_admin, "_pending_mark", mark or "")
        self.model.admin_map[key] = pending_admin
        self.model.dataChanged.emit(index, index, [Qt.UserRole])
        if hasattr(self, "table_view"):
            self.table_view.viewport().update()
        self.localBalanceChanged.emit()

    def _apply_committed_nurse_mark(self, index, admin, mark: str):
        if not self.model or not index.isValid() or admin is None:
            return
        key = self._admin_key_from_admin(admin)
        if key is None:
            return
        committed_admin = copy(admin)
        committed_admin.comment = mark or ""
        committed_admin.actual_time = datetime.now() if mark else None
        if hasattr(committed_admin, "_pending_mark"):
            delattr(committed_admin, "_pending_mark")
        self.model.admin_map[key] = committed_admin
        self.model.dataChanged.emit(index, index, [Qt.UserRole])
        if hasattr(self, "table_view"):
            self.table_view.viewport().update()
        self.localBalanceChanged.emit()

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if obj is self.table_view.viewport() and event.type() == QEvent.MouseButtonPress:
            index = self.table_view.indexAt(event.pos())
            if index.isValid():
                # Клик по ячейкам с назначениями (не первая колонка)
                if index.column() > 0:
                    admin = self.model.data(index, Qt.UserRole)
                    if admin and admin.status == "planned" and admin.cell_role in ("start", "single", "body", "end"):
                        mark = getattr(admin, "comment", "") or ""

                        # Проверка на раннюю отметку (> 1 часа в будущем)
                        planned_time_str = getattr(admin, "planned_time", None)
                        if planned_time_str:
                            if isinstance(planned_time_str, str):
                                planned_dt = datetime.fromisoformat(planned_time_str)
                            else:
                                planned_dt = planned_time_str

                            if datetime.now() < planned_dt - timedelta(minutes=60):
                                CustomMessageBox.custom_icon_message(
                                    self,
                                    "Предупреждение",
                                    "Но-но-но. Рановато. Не раньше чем за 1 час.",
                                    "rano.png"
                                )
                                return True

                        role = getattr(admin, "cell_role", "")
                        operation = None
                        next_mark = ""
                        admin_id = admin.id
                        click_seq = self._next_orders_click_seq()
                        if event.button() == Qt.LeftButton:
                            if role == "end":
                                logger.info(
                                    "[OrdersClick] click_ignore role=nurse seq=%s reason=end_cell admission_id=%s row=%s col=%s admin_id=%s",
                                    click_seq,
                                    self.admission_id,
                                    index.row(),
                                    index.column(),
                                    admin_id,
                                )
                                return True
                            if mark in (NURSE_MARK_EXECUTED, NURSE_MARK_NOT_EXECUTED):
                                operation = lambda aid=admin_id: self.service.cancel_nurse_order_mark(aid)
                            else:
                                next_mark = NURSE_MARK_EXECUTED
                                operation = lambda aid=admin_id: self.service.set_nurse_order_mark(aid, NURSE_MARK_EXECUTED)
                        elif event.button() == Qt.RightButton:
                            if mark in (NURSE_MARK_EXECUTED, NURSE_MARK_NOT_EXECUTED):
                                operation = lambda aid=admin_id: self.service.cancel_nurse_order_mark(aid)
                            else:
                                next_mark = NURSE_MARK_NOT_EXECUTED
                                operation = lambda aid=admin_id: self.service.set_nurse_order_mark(aid, NURSE_MARK_NOT_EXECUTED)

                        if operation:
                            logger.info(
                                "[OrdersClick] click_accept role=nurse seq=%s admission_id=%s row=%s col=%s admin_id=%s old_mark=%s next_mark=%s button=%s",
                                click_seq,
                                self.admission_id,
                                index.row(),
                                index.column(),
                                admin_id,
                                mark,
                                next_mark,
                                str(event.button()),
                            )
                            self._admin_only_snapshot_until = time.monotonic() + self._admin_only_snapshot_window_sec
                            key = self._admin_key_from_admin(admin)
                            previous_admin = copy(admin) if key is not None else None
                            self._apply_pending_nurse_mark(index, admin, next_mark)
                            self._begin_admin_write()

                            def on_success():
                                self._apply_committed_nurse_mark(index, admin, next_mark)
                                self._finish_admin_write()
                                self._on_mark_updated()

                            def on_error(exc):
                                self._finish_admin_write()
                                self._restore_admin_cell(index, key, previous_admin)

                            self._enqueue_write(
                                f"nurse_order_mark:{admin_id}:seq={click_seq}",
                                operation=operation,
                                on_success=on_success,
                                on_error=on_error,
                                block_ui=False,
                            )
                            return True
        return super().eventFilter(obj, event)

    def _on_table_clicked(self, index):
        if not index.isValid():
            return
            
        # Реагируем только на клик по первой колонке (препарат)
        if index.column() == 0:
            order = self.model.data(index, Qt.UserRole)
            if order:
                row = index.row()
                # Переключаем подсветку
                if self.highlighted_order_id == order.id:
                    self.highlighted_order_id = None
                    self.delegate.highlighted_row = None
                else:
                    self.highlighted_order_id = order.id
                    self.delegate.highlighted_row = row
                
                # Обновляем отображение таблицы (только её область)
                if hasattr(self, 'table_view'):
                    self.table_view.viewport().update()

    def _on_mark_updated(self):
        logger.info(
            "[OrdersClick] mark_updated_local role=nurse admission_id=%s",
            self.admission_id,
        )
        if self.model is not None:
            self._cached_has_administrations = self._model_has_administrations()
            self.check_drafts()
            if hasattr(self, "table_view"):
                self.table_view.viewport().update()
        else:
            self.check_drafts()
        self.orderMarked.emit()
        self._schedule_silent_sync()

    def _show_warning(self, text: str):
        CustomMessageBox.warning(self, "Предупреждение", text)
