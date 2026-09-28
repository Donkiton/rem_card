"""RefreshCoordinationMixin for the doctor orders widget."""

from PySide6.QtCore import QTimer
from datetime import datetime
from rem_card.app.local_metrics import record_metric
from rem_card.app.logger import logger
from rem_card.services.orders_sync_observability import record_orders_sync_event
from rem_card.ui.doctor_view.order_features.constants import ORDERS_POST_FINALIZE_WATCHDOG_MS
from rem_card.ui.shared.async_call import AsyncCallThread
from rem_card.ui.shared.orders_model import OrdersModel
import time


class RefreshCoordinationMixin:
    def request_refresh(self, *, force: bool = False, source: str = "refresh", priority: str = "HIGH"):
        logger.info(
            "[OrdersClick] request_refresh role=doctor admission_id=%s force=%s source=%s",
            self.admission_id,
            int(bool(force)),
            source,
        )
        self._request_snapshot(
            force=force,
            source=source,
            priority=priority,
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
            self._balance_mark_overrides.clear()
            self._detach_active_snapshot_for_context_switch(
                new_context_key=current_context_key,
                new_context_hash=None,
                replacement_request_id=None,
            )
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
            variant="full" if self._legacy_central_draft_detected else "committed",
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
        matched_prefixes = {
            prefix
            for source in sources
            for prefix in self._LOCAL_SILENT_FORCE_PREFIXES
            if source.startswith(prefix)
        }
        if not matched_prefixes:
            return False
        if "orders_finalize:" in matched_prefixes:
            try:
                payload_change_id = int(payload.get("last_change_id") or 0)
                applied_change_id = int(self._last_polled_change_id or 0)
            except (TypeError, ValueError):
                return False
            return payload_change_id > 0 and applied_change_id >= payload_change_id
        return True

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

    def _forced_reload_key(self, *, context_hash=None, reason: str):
        return (
            int(self.admission_id or 0),
            str(context_hash or "unknown"),
            str(reason or "unknown"),
        )

    def _record_forced_reload_metric(self, name: str, value=1, *, key=None, reason: str = "", **fields):
        admission_id, context_hash, key_reason = key or self._forced_reload_key(
            context_hash=fields.get("context_hash"),
            reason=reason,
        )
        record_metric(
            name,
            value,
            role="doctor",
            admission_id=admission_id,
            context_hash=context_hash,
            reason=reason or key_reason,
            **fields,
        )

    def _is_forced_reload_coalesced(self, *, key, now_ms: float, reason: str, guard_active: bool) -> bool:
        active_same = self._forced_reload_active_key == key
        pending_same = self._forced_reload_pending_key == key
        guard_deferred_same = self._forced_reload_after_guard_key == key
        last_ms = float((self._forced_reload_recent or {}).get(key) or 0.0)
        elapsed_ms = max(0.0, now_ms - last_ms) if last_ms > 0 else None
        cooldown_ms = float(self._forced_reload_cooldown_ms or 0)
        in_cooldown = elapsed_ms is not None and cooldown_ms > 0 and elapsed_ms < cooldown_ms

        suppress_reason = ""
        if active_same:
            suppress_reason = "active"
        elif pending_same:
            suppress_reason = "pending"
        elif guard_deferred_same:
            suppress_reason = "guard_deferred"
        elif in_cooldown:
            suppress_reason = "cooldown"

        if not suppress_reason:
            return False

        if in_cooldown:
            remaining_ms = max(0.0, cooldown_ms - float(elapsed_ms or 0.0))
            self._record_forced_reload_metric(
                "orders_forced_reload_cooldown_ms",
                round(remaining_ms, 3),
                key=key,
                reason=reason,
                suppress_reason=suppress_reason,
                guard_active=int(bool(guard_active)),
            )
        self._record_forced_reload_metric(
            "orders_forced_reload_coalesced",
            1,
            key=key,
            reason=reason,
            suppress_reason=suppress_reason,
            guard_active=int(bool(guard_active)),
        )
        self._record_forced_reload_metric(
            "orders_forced_reload_suppressed",
            1,
            key=key,
            reason=reason,
            suppress_reason=suppress_reason,
            guard_active=int(bool(guard_active)),
        )
        logger.debug(
            "[OrdersSync] forced_reload_after_stale_block_suppressed role=doctor admission_id=%s reason=%s suppress_reason=%s context_hash=%s",
            key[0],
            reason,
            suppress_reason,
            key[1],
        )
        return True

    def _enqueue_forced_reload(self, *, key, reason: str, log_warning: bool):
        self._forced_reload_pending_key = key
        pending_inflight = bool(self._snapshot_worker is not None)
        log = logger.warning if log_warning else logger.info
        log(
            "[OrdersSync] forced_reload_after_stale_block role=doctor admission_id=%s reason=%s pending_inflight=%s context_hash=%s",
            self.admission_id,
            reason,
            int(pending_inflight),
            key[1],
        )
        record_orders_sync_event(
            "forced_reload",
            role="doctor",
            admission_id=int(self.admission_id or 0),
            context_hash=key[1],
            reason=reason,
            immediate=bool(log_warning),
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

    def _flush_deferred_forced_reload_after_guard(self):
        if self._is_closing or self._local_cell_draft_guard:
            return
        key = self._forced_reload_after_guard_key
        reason = self._forced_reload_after_guard_reason
        if key is None:
            return
        self._forced_reload_after_guard_key = None
        self._forced_reload_after_guard_reason = None
        if self._forced_reload_active_key == key or self._forced_reload_pending_key == key:
            self._record_forced_reload_metric(
                "orders_forced_reload_coalesced",
                1,
                key=key,
                reason=str(reason or key[2]),
                suppress_reason="guard_release_already_scheduled",
                guard_active=0,
            )
            return
        self._enqueue_forced_reload(key=key, reason=str(reason or key[2]), log_warning=False)

    def _queue_forced_reload_after_stale_snapshot(self, *, reason: str):
        if self._is_closing:
            return
        pending_inflight = bool(self._snapshot_worker is not None)
        try:
            context_hash = self._build_orders_context().hash()
        except Exception:
            context_hash = None
        key = self._forced_reload_key(context_hash=context_hash, reason=reason)
        now_ms = time.monotonic() * 1000.0
        guard_active = bool(self._local_cell_draft_guard)
        self._record_forced_reload_metric(
            "orders_forced_reload_requested",
            1,
            key=key,
            reason=reason,
            pending_inflight=int(pending_inflight),
            guard_active=int(guard_active),
        )

        if self._is_forced_reload_coalesced(key=key, now_ms=now_ms, reason=reason, guard_active=guard_active):
            return

        self._forced_reload_recent[key] = now_ms
        while len(self._forced_reload_recent) > 32:
            oldest_key = next(iter(self._forced_reload_recent))
            self._forced_reload_recent.pop(oldest_key, None)

        if guard_active:
            self._forced_reload_after_guard_key = key
            self._forced_reload_after_guard_reason = reason
            self._record_forced_reload_metric(
                "orders_stale_block_guard_active",
                1,
                key=key,
                reason=reason,
                pending_inflight=int(pending_inflight),
            )
            logger.warning(
                "[OrdersSync] forced_reload_after_stale_block role=doctor admission_id=%s reason=%s pending_inflight=%s context_hash=%s deferred=local_cell_draft_guard",
                self.admission_id,
                reason,
                int(pending_inflight),
                context_hash,
            )
            return

        self._enqueue_forced_reload(key=key, reason=reason, log_warning=True)

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
        normalized_source = self._normalize_snapshot_source(source)
        forced_reload_key = None
        if bool(force) and normalized_source == "monitor" and str(source or "").strip().lower() == "stale_snapshot":
            forced_reload_key = self._forced_reload_pending_key or self._forced_reload_key(
                context_hash=context.hash(),
                reason=str(invalidate_reason or "stale_snapshot"),
            )

        if self._snapshot_worker is not None:
            worker_running = self._snapshot_worker.isRunning()
            request_id_preview = f"orders-ui-{self._snapshot_seq + 1}-{context.hash()[:6]}"
            if not worker_running:
                self._snapshot_worker = None
                self._retire_snapshot_worker_state(state="finished", reason="worker_not_running")
            elif self._detach_active_snapshot_for_context_switch(
                new_context_key=context_key,
                new_context_hash=context.hash(),
                replacement_request_id=request_id_preview,
            ):
                pass
            elif self._should_supersede_active_snapshot_worker(
                context_key=context_key,
                source=source,
                priority=priority_name,
            ):
                self._detach_snapshot_worker(
                    self._snapshot_worker,
                    state="superseded",
                    reason="higher_priority_request",
                    replacement_request_id=request_id_preview,
                )
            else:
                if (
                    worker_running
                    and self._is_request_covered_by_active(
                        context_key=context_key,
                        source=source,
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
                        "[OrdersWidget] skipped duplicate in-flight request admission_id=%s priority=%s force=%s source=%s context_hash=%s",
                        context.admission_id,
                        priority_name,
                        int(bool(force)),
                        source,
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

        if self._snapshot_worker is not None:
            # Defensive guard: supersede/detach above must clear the active worker before
            # a higher-priority request can start.
            return

        self._snapshot_seq += 1
        seq = self._snapshot_seq
        request_id = f"orders-ui-{seq}-{context.hash()[:6]}"
        request_generation = seq
        admission_id = context.admission_id
        shift_date = context.shift_date
        context_hash = context.hash()
        self._active_request_context_key = context_key
        self._active_request_force = bool(force)
        self._active_request_priority = priority_name
        self._active_request_seq = seq
        self._active_request_id = request_id
        self._active_request_generation = request_generation
        self._active_request_source = str(source or "refresh").strip().lower() or "refresh"
        self._active_request_started_monotonic = time.monotonic()
        self._forced_reload_active_key = forced_reload_key
        if forced_reload_key is not None and self._forced_reload_pending_key == forced_reload_key:
            self._forced_reload_pending_key = None
        self._active_snapshot_worker_state = {
            "request_id": request_id,
            "generation": request_generation,
            "source": self._active_request_source,
            "priority": priority_name,
            "admission_id": admission_id,
            "started_at": datetime.now().isoformat(timespec="milliseconds"),
            "started_monotonic": self._active_request_started_monotonic,
            "state": "active",
            "context_key": context_key,
            "seq": seq,
            "force": bool(force),
        }
        self._schedule_soft_update_state(source=source)
        if self._active_request_source == "post_finalize":
            self._set_refresh_status("Сохранено, обновляю назначения...")
        self._post_finalize_watchdog_timer.start(ORDERS_POST_FINALIZE_WATCHDOG_MS)

        def cancel_check():
            return (
                self._is_closing
                or str(self._active_request_id or "") != request_id
                or int(self._active_request_generation or 0) != request_generation
                or self._active_request_context_key != context_key
            )

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
                cancel_check=cancel_check,
            )
            return {
                "seq": seq,
                "admission_id": admission_id,
                "shift_date": shift_date,
                "context_key": context_key,
                "context_hash": context_hash,
                "priority": priority_name,
                "source": source,
                "request_id": request_id,
                "generation": request_generation,
                "snapshot_request_id": (snapshot or {}).get("load_trace_id"),
                "snapshot_generation": (snapshot or {}).get("generation", 0),
                "snapshot": snapshot,
            }

        self._snapshot_worker = AsyncCallThread(job)
        self._snapshot_worker.succeeded.connect(self._apply_snapshot)
        self._snapshot_worker.failed.connect(self._on_snapshot_failed)
        self._snapshot_worker.finished.connect(self._on_snapshot_finished)
        self._snapshot_worker.start()
