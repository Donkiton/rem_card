"""SnapshotApplicationMixin for the doctor orders widget."""

from PySide6.QtCore import QTimer
from datetime import datetime
from rem_card.app.local_metrics import record_metric
from rem_card.app.logger import logger
from rem_card.services.orders_sync_observability import record_orders_sync_event
from rem_card.services.read_coordinator import OrdersRefreshCancelled
from rem_card.ui.doctor_view.order_features.constants import ORDERS_POST_FINALIZE_MAX_RETRIES
from rem_card.ui.doctor_view.order_features.constants import ORDERS_POST_FINALIZE_RETRY_BACKOFF_MS
from rem_card.ui.doctor_view.order_features.constants import ORDERS_POST_FINALIZE_RETRY_MAX_BACKOFF_MS
from rem_card.ui.doctor_view.order_features.constants import ORDERS_POST_FINALIZE_WATCHDOG_MS
from rem_card.ui.shared.orders_balance_adapter import build_balance_orders_from_orders_widget
import time


class SnapshotApplicationMixin:
    def _apply_snapshot(self, payload):
        if self._is_closing:
            return
        try:
            if not isinstance(payload, dict):
                return
            payload_request_id = str(payload.get("request_id") or "")
            retired_state = self._retired_snapshot_worker_states.get(payload_request_id)
            if retired_state and str(retired_state.get("state") or "") in {"stalled", "superseded", "detached"}:
                self._record_late_result_ignored(payload, reason=f"retired_{retired_state.get('state')}")
                return
            if payload.get("seq") != self._snapshot_seq:
                self._record_late_result_ignored(payload, reason="seq_mismatch")
                logger.info(
                    "[OrdersWidget] discard stale snapshot seq request_seq=%s current_seq=%s context_hash=%s trace_id=%s",
                    payload.get("seq"),
                    self._snapshot_seq,
                    payload.get("context_hash"),
                    (payload.get("snapshot") or {}).get("load_trace_id"),
                )
                return
            expected_request_id = str(self._active_request_id or "")
            if expected_request_id and str(payload.get("request_id") or "") != expected_request_id:
                self._record_late_result_ignored(payload, reason="request_id_mismatch")
                return
            expected_generation = int(self._active_request_generation or 0)
            if expected_generation and int(payload.get("generation") or 0) != expected_generation:
                self._record_late_result_ignored(payload, reason="generation_mismatch")
                return
            if payload.get("admission_id") != self.admission_id:
                self._record_late_result_ignored(payload, reason="admission_mismatch")
                logger.info(
                    "[OrdersWidget] discard stale snapshot admission request_admission_id=%s current_admission_id=%s context_hash=%s trace_id=%s",
                    payload.get("admission_id"),
                    self.admission_id,
                    payload.get("context_hash"),
                    (payload.get("snapshot") or {}).get("load_trace_id"),
                )
                return
            applied = self._apply_snapshot_data(
                snapshot=payload.get("snapshot") or {},
                admission_id=payload.get("admission_id"),
                shift_date=payload.get("shift_date"),
                context_key=payload.get("context_key"),
            )
            if applied and str(payload.get("source") or "").strip().lower() == "post_finalize":
                self._post_finalize_retry_count = 0
        except Exception:
            logger.exception("[OrdersWidget] Failed to apply orders snapshot")

    def _record_late_result_ignored(self, payload, *, reason: str):
        snapshot = (payload or {}).get("snapshot") or {}
        logger.info(
            "[OrdersWidget] orders_refresh_late_result_ignored reason=%s admission_id=%s request_id=%s generation=%s current_request_id=%s current_generation=%s context_hash=%s trace_id=%s",
            reason,
            (payload or {}).get("admission_id"),
            (payload or {}).get("request_id"),
            (payload or {}).get("generation"),
            self._active_request_id,
            self._active_request_generation,
            (payload or {}).get("context_hash"),
            snapshot.get("load_trace_id"),
        )
        record_metric(
            "orders_refresh_late_result_ignored",
            1,
            admission_id=(payload or {}).get("admission_id"),
            source=str((payload or {}).get("source") or "refresh"),
            reason=reason,
            request_id=(payload or {}).get("request_id"),
            generation=(payload or {}).get("generation"),
            current_request_id=self._active_request_id,
            current_generation=self._active_request_generation,
            context_hash=(payload or {}).get("context_hash"),
            trace_id=snapshot.get("load_trace_id"),
        )

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
            snapshot_source = str(snapshot.get("source") or "refresh").strip().lower()
            metric_source = {
                "user": "click",
                "click": "click",
                "post_finalize": "post_finalize",
                "cache": "cache",
            }.get(snapshot_source, "monitor")
            record_metric(
                "orders_snapshot_apply_skipped",
                1,
                admission_id=admission_id,
                source=metric_source,
                snapshot_source=snapshot_source,
                reason="duplicate_snapshot",
                context_hash=snapshot.get("context_hash"),
                trace_id=snapshot.get("load_trace_id"),
                version=snapshot.get("version"),
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
        self._apply_full_snapshot_to_model(snapshot)
        legacy_preflight_detected = bool(
            snapshot.get("only_committed", False)
            and snapshot.get("has_any_draft", False)
            and not self._has_local_draft_changes()
        )
        if legacy_preflight_detected:
            self._legacy_central_draft_detected = True
        elif not snapshot.get("has_any_draft", False):
            self._legacy_central_draft_detected = False
        self.set_forced_read_only(self._forced_read_only)
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
        self._emit_committed_orders_balance_ready(
            snapshot,
            admission_id=admission_id,
            shift_date=shift_date,
            change_id=snapshot_change_id,
        )
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
        if legacy_preflight_detected:
            QTimer.singleShot(
                0,
                lambda: self._request_snapshot(
                    force=True,
                    source="legacy_central_draft_review",
                    priority="HIGH",
                    invalidate_reason="legacy_central_draft_review",
                ) if not self._is_closing and self._legacy_central_draft_detected else None,
            )
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

    def _emit_committed_orders_balance_ready(
        self,
        snapshot,
        *,
        admission_id,
        shift_date,
        change_id: int,
    ) -> None:
        if not bool(snapshot.get("only_committed", False)) or bool(snapshot.get("has_any_draft", False)):
            return
        balance_orders = build_balance_orders_from_orders_widget(
            self,
            admission_id,
            shift_date,
            tab_active=True,
        )
        if balance_orders is None:
            return
        self.committedOrdersBalanceReady.emit(
            {
                "admission_id": int(admission_id or 0),
                "shift_date": shift_date,
                "change_id": int(change_id or 0),
                "source": str(snapshot.get("source") or ""),
                "orders": balance_orders,
            }
        )

    def _snapshot_apply_signature(self, snapshot, context_key):
        try:
            return (
                context_key or snapshot.get("cache_key"),
                int(snapshot.get("version") or snapshot.get("change_id") or 0),
                str(snapshot.get("content_hash") or ""),
                str(snapshot.get("dedup_signature") or ""),
            )
        except Exception:
            return None

    def _apply_full_snapshot_to_model(self, snapshot):
        table = getattr(self, "table_view", None)
        previous_signals = None
        sorting_enabled = False
        if table is not None:
            try:
                previous_signals = table.blockSignals(True)
            except Exception:
                previous_signals = None
            try:
                sorting_enabled = bool(table.isSortingEnabled())
                if sorting_enabled:
                    table.setSortingEnabled(False)
            except Exception:
                sorting_enabled = False
        try:
            self.model.apply_snapshot(snapshot)
            self._capture_local_draft_baseline(snapshot)
        finally:
            if table is not None:
                try:
                    if sorting_enabled:
                        table.setSortingEnabled(True)
                except Exception:
                    pass
                try:
                    if previous_signals is not None:
                        table.blockSignals(previous_signals)
                except Exception:
                    pass

    def _on_snapshot_failed(self, exc):
        if self._is_closing:
            return
        if isinstance(exc, OrdersRefreshCancelled):
            request_source = str(self._active_request_source or "refresh").strip().lower()
            if request_source == "post_finalize":
                try:
                    context_hash = self._build_orders_context().hash()
                except Exception:
                    context_hash = None
                self._post_finalize_retry_after_cancel = True
                self._post_finalize_retry_context_hash = context_hash
                logger.info("[OrdersWidget] post_finalize snapshot load cancelled; retry will be scheduled: %s", exc)
                return
            self._clear_soft_update_state()
            logger.info("[OrdersWidget] Orders snapshot load cancelled: %s", exc)
            return
        self._clear_soft_update_state()
        logger.warning("[OrdersWidget] Orders snapshot load failed: %s", exc, exc_info=True)

    def _on_post_finalize_snapshot_watchdog(self):
        if self._is_closing:
            return
        worker = self._snapshot_worker
        if worker is None or not worker.isRunning():
            return

        elapsed_ms = max(0.0, (time.monotonic() - self._active_request_started_monotonic) * 1000.0)
        stale_seq = int(self._active_request_seq or 0)
        request_source = str(self._active_request_source or "refresh")
        request_id = str(self._active_request_id or "")
        state = dict(getattr(self, "_active_snapshot_worker_state", {}) or {})
        if state:
            state["state"] = "stalled"
            state["stalled_at"] = datetime.now().isoformat(timespec="milliseconds")
            self._active_snapshot_worker_state = state
        try:
            context = self._build_orders_context()
            context_hash = context.hash()
        except Exception:
            context = None
            context_hash = None
        logger.warning(
            "[OrdersWidget] snapshot_worker_stalled admission_id=%s seq=%s request_id=%s source=%s elapsed_ms=%.2f context_hash=%s",
            self.admission_id,
            stale_seq,
            request_id,
            request_source,
            elapsed_ms,
            context_hash,
        )
        record_metric(
            "orders_snapshot_worker_stalled",
            round(elapsed_ms, 3),
            admission_id=self.admission_id,
            source=request_source,
            request_id=request_id,
            seq=stale_seq,
            context_hash=context_hash,
            threshold_ms=ORDERS_POST_FINALIZE_WATCHDOG_MS,
        )
        if request_source == "post_finalize":
            record_metric(
                "orders_post_finalize_snapshot_stalled",
                round(elapsed_ms, 3),
                admission_id=self.admission_id,
                source="post_finalize",
                request_id=request_id,
                seq=stale_seq,
                context_hash=context_hash,
                threshold_ms=ORDERS_POST_FINALIZE_WATCHDOG_MS,
            )

        self._detach_snapshot_worker(
            worker,
            state="detached",
            reason="watchdog_stalled",
        )
        self._snapshot_seq += 1
        self._clear_soft_update_state()
        if request_source == "post_finalize" and context is not None:
            self._set_refresh_status("Сохранено, данные обновляются...")
            self._schedule_post_finalize_retry(context_hash=context_hash)
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
        else:
            self._flush_deferred_forced_reload_after_guard()

    def _schedule_post_finalize_retry(self, *, context_hash=None):
        if self._is_closing:
            return
        if self._post_finalize_retry_count >= ORDERS_POST_FINALIZE_MAX_RETRIES:
            logger.warning(
                "[OrdersWidget] post_finalize_retry_limit admission_id=%s retries=%s context_hash=%s",
                self.admission_id,
                self._post_finalize_retry_count,
                context_hash,
            )
            record_metric(
                "orders_post_finalize_retry_limit",
                1,
                admission_id=self.admission_id,
                source="post_finalize",
                retries=self._post_finalize_retry_count,
                context_hash=context_hash,
            )
            self._set_refresh_status("Сохранено. Не удалось обновить список назначений автоматически.")
            return
        self._post_finalize_retry_count += 1
        retry_index = self._post_finalize_retry_count
        delay_ms = 0
        if retry_index > 1:
            delay_ms = min(
                ORDERS_POST_FINALIZE_RETRY_MAX_BACKOFF_MS,
                ORDERS_POST_FINALIZE_RETRY_BACKOFF_MS * (2 ** max(0, retry_index - 2)),
            )
        logger.warning(
            "[OrdersWidget] post_finalize_retry_scheduled admission_id=%s retry=%s delay_ms=%s context_hash=%s",
            self.admission_id,
            retry_index,
            delay_ms,
            context_hash,
        )
        record_metric(
            "orders_post_finalize_retry_scheduled",
            1,
            admission_id=self.admission_id,
            source="post_finalize",
            retry=retry_index,
            delay_ms=delay_ms,
            context_hash=context_hash,
        )
        QTimer.singleShot(
            delay_ms,
            lambda: self._request_snapshot(
                force=True,
                source="post_finalize",
                priority="HIGH",
                invalidate_reason=f"post_finalize_retry_{retry_index}",
            ),
        )

    def _on_snapshot_finished(self):
        worker = self.sender()
        if worker is not None and self._snapshot_worker is not worker:
            return
        self._post_finalize_watchdog_timer.stop()
        self._snapshot_worker = None
        self._retire_snapshot_worker_state(state="finished", reason="worker_finished")
        self._reset_active_snapshot_request()
        self._clear_soft_update_state()
        if self._is_closing:
            self._reset_pending_snapshot_request()
            return
        if self._post_finalize_retry_after_cancel:
            context_hash = self._post_finalize_retry_context_hash
            self._post_finalize_retry_after_cancel = False
            self._post_finalize_retry_context_hash = None
            try:
                context = self._build_orders_context()
                context_hash = context_hash or context.hash()
            except Exception:
                context = None
            self._set_refresh_status("Сохранено, данные обновляются...")
            self._schedule_post_finalize_retry(context_hash=context_hash)
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
        else:
            self._flush_deferred_forced_reload_after_guard()

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

    @staticmethod
    def _normalize_snapshot_source(value: str) -> str:
        source = str(value or "refresh").strip().lower() or "refresh"
        if source in {"user", "click", "user_click"}:
            return "click"
        if source in {"post_finalize", "cache", "monitor", "background", "visible_tab", "refresh"}:
            return source
        if source in {"local_silent_sync", "stale_snapshot", "poll_external_updates"}:
            return "monitor"
        return "refresh"

    @classmethod
    def _snapshot_request_rank(cls, source: str, priority: str) -> int:
        priority_rank = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}.get(cls._normalize_priority(priority), 1)
        source_rank = {
            "cache": 0,
            "monitor": 0,
            "background": 0,
            "refresh": 1,
            "visible_tab": 2,
            "click": 2,
            "post_finalize": 3,
        }.get(cls._normalize_snapshot_source(source), 1)
        return source_rank * 10 + priority_rank

    def _should_supersede_active_snapshot_worker(self, *, context_key, source: str, priority: str) -> bool:
        state = getattr(self, "_active_snapshot_worker_state", {}) or {}
        if not state or state.get("context_key") != context_key:
            return False
        active_state = str(state.get("state") or "active")
        if active_state not in {"active", "stalled"}:
            return False
        incoming_rank = self._snapshot_request_rank(source, priority)
        active_rank = self._snapshot_request_rank(
            str(state.get("source") or self._active_request_source),
            str(state.get("priority") or self._active_request_priority),
        )
        return incoming_rank > active_rank

    def _is_request_covered_by_active(self, *, context_key, source: str, force: bool, priority: str) -> bool:
        if self._active_request_context_key != context_key:
            return False
        state = getattr(self, "_active_snapshot_worker_state", {}) or {}
        active_rank = self._snapshot_request_rank(
            str(state.get("source") or self._active_request_source),
            str(state.get("priority") or self._active_request_priority),
        )
        incoming_rank = self._snapshot_request_rank(source, priority)
        if self._active_request_force and not force:
            return True
        if self._active_request_force == bool(force) and active_rank >= incoming_rank:
            return True
        return False
