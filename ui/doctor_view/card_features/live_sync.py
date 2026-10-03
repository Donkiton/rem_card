from __future__ import annotations

from rem_card.ui.shared.balance_snapshot_sync import BalanceSnapshotSync
from copy import deepcopy
from rem_card.app.logger import logger
from .constants import CARD_CACHE_CHANGE_ENTITIES, EMERGENCY_NOTICE_FORCE_PREFIX, LAB_ORDER_CHANGE_ENTITIES, LOCAL_ORDER_FORCE_PREFIXES, ORDER_CHANGE_ENTITIES, VITALS_CACHE_CHANGE_ENTITIES

class DoctorLiveSyncMixin:
    def _reset_balance_view_state(self):
        if hasattr(self, "balance_controller") and self.balance_controller:
            self.balance_controller.hourly_cache = self.balance_controller._build_empty_hourly_cache()
            self.balance_controller._effective_bounds_cache = None
            if getattr(self.balance_controller, "quick_input", None):
                quick_input = self.balance_controller.quick_input
                if hasattr(quick_input, "set_loading_state"):
                    quick_input.set_loading_state()
                else:
                    quick_input.update_quick_values({})

        sector_2b_g = getattr(self.layout_manager, "sector_2b_g", None)
        if sector_2b_g is not None:
            if hasattr(sector_2b_g, "set_loading_state"):
                sector_2b_g.set_loading_state()
            else:
                sector_2b_g.update_values()
        sector_2b_v = getattr(self.layout_manager, "sector_2b_v", None)
        if sector_2b_v is not None:
            if hasattr(sector_2b_v, "set_loading_state"):
                sector_2b_v.set_loading_state()
            else:
                sector_2b_v.update_balance(0, 0, 0, 0)
                sector_2b_v.update_quick_values({})
        sector_3a = getattr(self.layout_manager, "sector_3a", None)
        if sector_3a is not None:
            if hasattr(sector_3a, "set_loading_state"):
                sector_3a.set_loading_state()
            else:
                sector_3a.update_values(0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        sector_3b = getattr(self.layout_manager, "sector_3b", None)
        if sector_3b is not None:
            if hasattr(sector_3b, "set_loading_state"):
                sector_3b.set_loading_state()
            else:
                sector_3b.update_values(0, {})
        sector_4a = getattr(self.layout_manager, "sector_4a", None)
        if sector_4a is not None:
            if hasattr(sector_4a, "set_loading_state"):
                sector_4a.set_loading_state()
            else:
                sector_4a.update_balance(0, 0, 0, 0)

    def _payload_is_relevant(self, payload: dict) -> bool:
        if self._archive_read_only_mode or not self.admission_id:
            return False
        if payload.get("forced"):
            return True

        relevant_entities = {
            "patients",
            "admissions",
            "beds",
            "operations",
            "diet_templates",
            "patient_status_events",
        } | LAB_ORDER_CHANGE_ENTITIES
        orders_entities = {"orders", "administrations", "lab_orders"}
        for change in payload.get("changes") or []:
            admission_id = change.get("admission_id")
            entity_name = str(change.get("entity_name") or "")
            if admission_id is not None and int(admission_id) == int(self.admission_id):
                return True
            if entity_name in orders_entities and admission_id is None:
                return True
            if entity_name in relevant_entities:
                return True
        changed_entities = {
            str(entity)
            for entity in (payload.get("changed_entities") or [])
            if entity is not None
        }
        if changed_entities.intersection(relevant_entities):
            return True
        if changed_entities.intersection(orders_entities | {"diet_templates"}) and not payload.get("changes"):
            return True
        return False

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

    def _is_local_orders_force_payload(self, payload: dict, changed_entities: set[str]) -> bool:
        if not payload.get("forced"):
            return False
        sources = self._payload_force_sources(payload)
        if not sources:
            return False
        if changed_entities and not set(changed_entities).issubset(ORDER_CHANGE_ENTITIES):
            return False
        return any(
            source.startswith(prefix)
            for source in sources
            for prefix in LOCAL_ORDER_FORCE_PREFIXES
        )

    def _is_local_emergency_notice_payload(self, payload: dict, changed_entities: set[str]) -> bool:
        if not payload.get("forced"):
            return False
        sources = self._payload_force_sources(payload)
        if not any(source.startswith(EMERGENCY_NOTICE_FORCE_PREFIX) for source in sources):
            return False
        return set(changed_entities).issubset({"admissions"})

    def _invalidate_vitals_cache_from_payload(self, payload: dict, changed_entities: set[str]) -> None:
        if self._archive_read_only_mode:
            return
        force_sources = self._payload_force_sources(payload)
        vitals_entities = changed_entities.intersection(VITALS_CACHE_CHANGE_ENTITIES)
        card_entities = changed_entities.intersection(CARD_CACHE_CHANGE_ENTITIES)
        invalidate_vitals = bool(vitals_entities)
        invalidate_card = bool(card_entities)
        if payload.get("forced") and force_sources:
            if self._is_local_orders_force_payload(payload, changed_entities):
                invalidate_card = True
            elif self._is_local_emergency_notice_payload(payload, changed_entities):
                invalidate_card = True
            else:
                invalidate_vitals = True
                invalidate_card = True
        if not (invalidate_vitals or invalidate_card):
            return
        coordinator = self._get_read_coordinator()
        if coordinator is None:
            return

        admission_ids = {
            int(admission_id)
            for admission_id in (payload.get("admission_ids") or [])
            if admission_id is not None
        }
        for change in payload.get("changes") or []:
            entity_name = str(change.get("entity_name") or "")
            admission_id = change.get("admission_id")
            if (
                entity_name in (VITALS_CACHE_CHANGE_ENTITIES | CARD_CACHE_CHANGE_ENTITIES)
                and admission_id is not None
            ):
                admission_ids.add(int(admission_id))

        if not admission_ids and (invalidate_vitals or invalidate_card):
            current_admission_id = getattr(self, "admission_id", None)
            if current_admission_id:
                admission_ids.add(int(current_admission_id))

        reason = f"data_changes:{','.join(sorted(changed_entities)) or ','.join(force_sources) or 'forced'}"
        for admission_id in admission_ids:
            vitals_removed = 0
            card_removed = 0
            if invalidate_vitals and hasattr(coordinator, "invalidate_patient_vitals_for_admission"):
                vitals_removed = coordinator.invalidate_patient_vitals_for_admission(
                    admission_id,
                    reason=reason,
                )
            if invalidate_card and hasattr(coordinator, "invalidate_patient_card_for_admission"):
                card_removed = coordinator.invalidate_patient_card_for_admission(
                    admission_id,
                    reason=reason,
                )
            logger.info(
                "DoctorRemCardWidget invalidated patient snapshot cache admission_id=%s vitals_entries=%s card_entries=%s reason=%s",
                admission_id,
                vitals_removed,
                card_removed,
                reason,
            )

    def _refresh_balance_from_db(self) -> None:
        if hasattr(self, "_balance_snapshot_sync"):
            self._balance_snapshot_sync.schedule()

    def _balance_snapshot_context(self):
        if self._is_closing or self._selection_mode != "card" or not self.admission_id or self._current_date is None:
            return None
        return (
            int(self.admission_id),
            self._current_date,
            "archive" if self._archive_read_only_mode else "live",
            self._archive_source_db_path if self._archive_read_only_mode else "live",
            self.service,
        )

    def _balance_mark_override_sequence(self) -> int:
        widget = getattr(getattr(self, "layout_manager", None), "orders_widget", None)
        getter = getattr(widget, "balance_mark_override_sequence", None)
        return int(getter() or 0) if callable(getter) else 0

    def _load_balance_snapshot_job(self, request: dict) -> dict:
        admission_id, shift_date, mode, source_db, service = request["context"]
        coordinator = getattr(service, "read_coordinator", None)
        if coordinator is not None and hasattr(coordinator, "load_balance_snapshot"):
            return coordinator.load_balance_snapshot(
                admission_id,
                shift_date,
                role="doctor",
                mode=mode,
                source_db=source_db,
                balance_only_committed=True,
                force_refresh=True,
            )
        return service.build_balance_snapshot(
            admission_id,
            shift_date,
            include_change_cursor=True,
            balance_only_committed=True,
        )

    def _apply_authoritative_balance_snapshot(self, snapshot: dict, request: dict) -> None:
        if not snapshot:
            return
        cached_snapshot = dict(self._card_snapshot_cache or {})
        for key in BalanceSnapshotSync.BALANCE_FIELDS:
            if key in snapshot:
                cached_snapshot[key] = snapshot.get(key)
        self._card_snapshot_cache = cached_snapshot
        if snapshot.get("balance_runtime") is not None:
            self._balance_runtime_cache = snapshot.get("balance_runtime")
            self._balance_runtime_provisional = False
        self._last_change_id = max(
            int(self._last_change_id or 0),
            int(snapshot.get("change_id") or snapshot.get("version") or 0),
        )
        widget = getattr(getattr(self, "layout_manager", None), "orders_widget", None)
        acknowledge = getattr(widget, "acknowledge_balance_mark_overrides", None)
        if callable(acknowledge):
            acknowledge(request.get("overlay_sequence") or 0)
        if (
            hasattr(self, "balance_controller")
            and snapshot.get("effective_bounds")
            and snapshot.get("fluids") is not None
        ):
            self.balance_controller.apply_loaded_data(
                snapshot.get("fluids") or [],
                snapshot.get("effective_bounds"),
            )
        else:
            self.update_balance_data()

    def _refresh_status_from_db(self) -> None:
        try:
            if hasattr(self.layout_manager, "refresh_current_status"):
                self.layout_manager.refresh_current_status()
        except Exception:
            logger.exception("Doctor status partial refresh failed")

    def _refresh_ivl_from_db(self) -> None:
        try:
            sector_ivl = getattr(self.layout_manager, "sector_ivl", None)
            if sector_ivl is not None and hasattr(sector_ivl, "refresh"):
                sector_ivl.refresh(force=True)
        except Exception:
            logger.exception("Doctor IVL partial refresh failed")

    def _refresh_procedures_from_db(self) -> None:
        try:
            sector_proc = getattr(self.layout_manager, "sector_proc", None)
            if sector_proc is not None and hasattr(sector_proc, "refresh"):
                sector_proc.refresh()
        except Exception:
            logger.exception("Doctor procedures partial refresh failed")

    def _sync_lab_orders_context(self, *, force_refresh=False) -> bool:
        try:
            layout = getattr(self, "layout_manager", None)
            if layout is None:
                return False
            layout.current_admission_id = self.admission_id
            layout.current_date = self._current_date
            sector_anal = getattr(layout, "sector_anal", None)
            if sector_anal is None:
                return False
            if not self.admission_id or self._current_date is None:
                if hasattr(sector_anal, "clear_context"):
                    sector_anal.clear_context()
                elif hasattr(sector_anal, "set_lab_orders"):
                    sector_anal.set_lab_orders([])
                return True
            if hasattr(sector_anal, "set_context"):
                sector_anal.set_context(self.service, self.admission_id, self._current_date,
                                        force_refresh=force_refresh)
                return True
            if hasattr(sector_anal, "refresh"):
                sector_anal.refresh()
                return True
        except Exception:
            logger.exception("Doctor lab orders context sync failed")
        return False

    def _refresh_labs_from_db(self) -> None:
        try:
            self._sync_lab_orders_context(force_refresh=True)
        except Exception:
            logger.exception("Doctor lab orders partial refresh failed")

    def _refresh_emergency_notice_from_db(self) -> None:
        try:
            layout = getattr(self, "layout_manager", None)
            sector = getattr(layout, "sector_7vit_b", None) if layout is not None else None
            if sector is None:
                return
            if hasattr(sector, "set_context") and self.admission_id:
                sector.set_context(self.service, self.admission_id, self._current_date)
            if hasattr(sector, "refresh"):
                sector.refresh()
        except Exception:
            logger.exception("Doctor emergency notice partial refresh failed")

    @staticmethod
    def _changed_entities_from_payload(payload: dict) -> set[str]:
        changed_entities = {
            str(entity)
            for entity in (payload.get("changed_entities") or [])
            if entity is not None
        }
        if changed_entities:
            return changed_entities
        return {
            str(change.get("entity_name") or "")
            for change in (payload.get("changes") or [])
            if change.get("entity_name")
        }

    def _handle_diet_sync(
        self,
        payload: dict,
        changed_entities: set[str],
        *,
        full_refresh_required: bool,
        diet_refresh: bool,
    ) -> bool:
        diet_widget = getattr(self, "diet_intake_widget", None)
        if diet_widget is None:
            return False
        diet_entities = {"diet_templates", "diet_plan", "diet_plan_versions", "oral_intake_events"}
        has_diet_changes = bool(changed_entities.intersection(diet_entities))
        if full_refresh_required or diet_refresh:
            diet_widget.handle_data_changes(payload)
            return False
        if not has_diet_changes:
            return False
        diet_widget.handle_data_changes(payload)
        if "oral_intake_events" in changed_entities:
            self.update_balance_data()
        return set(changed_entities).issubset(diet_entities)

    def _refresh_orders_from_payload(
        self,
        payload: dict,
        *,
        full_refresh_required: bool,
        has_orders_changes: bool,
        orders_refresh: bool,
    ) -> None:
        current_orders_visibility_changes = bool(
            self._changed_entities_from_payload(payload).intersection(
                {"admissions", "patient_status_events"}
            )
        )
        should_refresh = full_refresh_required or has_orders_changes or orders_refresh
        if not should_refresh:
            if current_orders_visibility_changes:
                self._refresh_current_orders_from_payload(payload)
            return
        if hasattr(self.layout_manager, 'orders_widget'):
            try:
                self.layout_manager.orders_widget.handle_data_changes(
                    payload,
                    tab_active=self._is_orders_tab_active(),
                )
            except Exception:
                logger.exception("Orders delta refresh failed")
        self._refresh_current_orders_from_payload(payload)

    def _refresh_current_orders_from_payload(self, payload: dict) -> None:
        layout = getattr(self, "layout_manager", None)
        mgr = getattr(layout, "nurse_orders_manager", None) if layout is not None else None
        if mgr is None or not hasattr(mgr, "handle_data_changes"):
            return
        try:
            mgr.handle_data_changes(payload)
        except Exception:
            logger.exception("Current nurse orders refresh failed")

    def _apply_partial_sync_actions(self, sync_actions: dict, *, full_refresh_required: bool) -> None:
        if full_refresh_required:
            return
        if sync_actions.get("balance_refresh"):
            self._refresh_balance_from_db()
        if sync_actions.get("status_refresh"):
            self._refresh_status_from_db()
        if sync_actions.get("ivl_refresh"):
            self._refresh_ivl_from_db()
        if sync_actions.get("procedures_refresh"):
            self._refresh_procedures_from_db()
        if sync_actions.get("lab_orders_refresh"):
            self._refresh_labs_from_db()
        if sync_actions.get("emergency_notice_refresh"):
            self._refresh_emergency_notice_from_db()

    def _on_data_changes(self, payload: dict):
        if self._is_closing or not self.admission_id:
            return
        sync_actions = payload.get("sync_actions") or {}
        full_refresh_required = bool(sync_actions.get("full_refresh_required"))
        card_snapshot_required = bool(sync_actions.get("card_snapshot_required"))
        vitals_snapshot_required = bool(sync_actions.get("vitals_snapshot_required"))
        changed_entities = self._changed_entities_from_payload(payload)
        self._invalidate_vitals_cache_from_payload(payload, changed_entities)
        orders_entities = {"orders", "administrations", "lab_orders"}
        if self._selection_mode == "archive" and (
            full_refresh_required or changed_entities.intersection({"patients", "admissions"})
        ):
            try:
                if hasattr(self.layout_manager, "_refresh_archive_if_needed"):
                    self.layout_manager._refresh_archive_if_needed(force=True)
            except Exception:
                logger.exception("Archive refresh failed")
            return

        if not self._payload_is_relevant(payload):
            return
        if (
            hasattr(self, 'layout_manager')
            and hasattr(self.layout_manager, 'selection_stack')
            and self.layout_manager.selection_stack.currentIndex() != 0
        ):
            return
        if self._is_local_emergency_notice_payload(payload, changed_entities):
            logger.info(
                "DoctorRemCardWidget skipped card snapshot after local emergency notice save admission_id=%s sources=%s",
                self.admission_id,
                self._payload_force_sources(payload),
            )
            return
        if self._is_local_orders_force_payload(payload, changed_entities):
            self._balance_snapshot_sync.schedule(payload.get("last_change_id", 0))
            if hasattr(self.layout_manager, 'orders_widget'):
                try:
                    self.layout_manager.orders_widget.handle_data_changes(
                        payload,
                        tab_active=self._is_orders_tab_active(),
                    )
                except Exception:
                    logger.exception("Orders local forced skip failed")
            self._refresh_current_orders_from_payload(payload)
            logger.info(
                "[OrdersClick] skip local forced card snapshot role=doctor admission_id=%s sources=%s entities=%s",
                self.admission_id,
                self._payload_force_sources(payload),
                sorted(changed_entities),
            )
            self._schedule_balance_update()
            return

        if sync_actions.get("balance_refresh") or full_refresh_required:
            self._balance_snapshot_sync.schedule(payload.get("last_change_id", 0))

        if "admissions" in changed_entities and sync_actions.get("patient_header_refresh"):
            self._refresh_emergency_notice_from_db()

        if self._handle_diet_sync(
            payload,
            changed_entities,
            full_refresh_required=full_refresh_required,
            diet_refresh=bool(sync_actions.get("diet_refresh")),
        ):
            return
        has_orders_changes = bool(changed_entities.intersection(orders_entities))
        self._refresh_orders_from_payload(
            payload,
            full_refresh_required=full_refresh_required,
            has_orders_changes=has_orders_changes,
            orders_refresh=bool(sync_actions.get("orders_refresh")),
        )
        self._apply_partial_sync_actions(sync_actions, full_refresh_required=full_refresh_required)
        if full_refresh_required or card_snapshot_required:
            self._request_card_snapshot(show_empty_message=False)
        elif vitals_snapshot_required:
            if self._current_status_is_outcome():
                logger.info(
                    "DoctorRemCardWidget skipped vitals snapshot after outcome admission_id=%s sources=%s entities=%s",
                    self.admission_id,
                    self._payload_force_sources(payload),
                    sorted(changed_entities),
                )
            else:
                self._request_card_snapshot(show_empty_message=False, load_scope="patient_open_vitals")

    def start_polling(self):
        """Подписывает карту на сервисный monitor и оставляет только чистый UI-таймер баланса."""
        if self._is_closing or not self.admission_id:
            return
        if self._archive_read_only_mode:
            self.stop_polling()
            return
        self._ensure_monitor_subscription()
        if not self.balance_timer.isActive():
            self.balance_timer.start(60000)
        data_service = self._get_data_service()
        if data_service:
            data_service.request_immediate_refresh(force_emit=False, source="patient_open_polling")

    def stop_polling(self):
        self.balance_timer.stop()

    def _is_orders_tab_active(self) -> bool:
        return (
            hasattr(self.layout_manager, 'vitals_stack')
            and self.layout_manager.vitals_stack.currentIndex() == 1
            and hasattr(self.layout_manager, 'orders_widget')
        )

    def _get_report_controller(self):
        if self.report_controller is None or getattr(self.report_controller, "service", None) is not self.service:
            from rem_card.ui.shared.report_controller import RemCardReportController

            self.report_controller = RemCardReportController(self.service, self)
        return self.report_controller

    def _schedule_balance_update(self, *_args):
        if not self.admission_id:
            return
        self._balance_update_timer.start(self._balance_update_delay_ms)

    def _initialize_provisional_balance_runtime(self, start_dt, end_dt) -> None:
        self._balance_runtime_cache = {
            "orders": [],
            "start_dt": start_dt,
            "end_dt": end_dt,
            "transfer_time": None,
            "active_intervals": [],
            "outcome_time": None,
            "oral_events": [],
            "oral_plan_schedule": [],
            "oral_shift_date": self._current_date,
            "oral_start_dt": start_dt,
            "oral_end_dt": end_dt,
            "oral_totals": {
                "actual": 0.0,
                "planned": 0.0,
                "current": 0.0,
                "daily": 0.0,
            },
        }
        self._balance_runtime_provisional = True

    def _accept_committed_orders_balance_baseline(self, payload) -> None:
        payload = dict(payload or {})
        try:
            admission_id = int(payload.get("admission_id") or 0)
        except (TypeError, ValueError):
            return
        if (
            admission_id != int(self.admission_id or 0)
            or payload.get("shift_date") != self._current_date
        ):
            return

        runtime = dict(self._balance_runtime_cache or {})
        if not runtime.get("start_dt") or not runtime.get("end_dt"):
            try:
                start_dt, end_dt = self.service.get_day_period(self._current_date)
            except Exception:
                logger.warning(
                    "Failed to initialize balance runtime from committed orders admission_id=%s",
                    admission_id,
                    exc_info=True,
                )
                return
            self._initialize_provisional_balance_runtime(start_dt, end_dt)
            runtime = dict(self._balance_runtime_cache or {})

        runtime["orders"] = deepcopy(list(payload.get("orders") or []))
        self._balance_runtime_cache = runtime
        cached_snapshot = dict(self._card_snapshot_cache or {})
        cached_snapshot["balance_runtime"] = runtime
        self._card_snapshot_cache = cached_snapshot

        change_id = int(payload.get("change_id") or 0)
        if str(payload.get("source") or "").strip().lower() == "post_finalize":
            self._balance_snapshot_sync.schedule(change_id)

    def _bind_nurse_orders_balance_signals(self):
        if self._nurse_orders_balance_signals_bound:
            return
        mgr = getattr(getattr(self, "layout_manager", None), "nurse_orders_manager", None)
        if mgr is None:
            return
        if hasattr(mgr, "localBalanceChanged"):
            mgr.localBalanceChanged.connect(self.update_balance_data)
        if hasattr(mgr, "balanceRefreshRequested"):
            mgr.balanceRefreshRequested.connect(self._refresh_balance_from_db)
        self._nurse_orders_balance_signals_bound = True

    def _flush_scheduled_balance_update(self):
        if not self.admission_id:
            return
        self.update_balance_data()

    def _local_oral_events_for_balance(self):
        state = self._local_oral_state_for_balance()
        if state is None:
            return None
        return state[0]

    def _local_oral_state_for_balance(self):
        widget = getattr(self, "diet_intake_widget", None)
        if widget is None:
            return None
        try:
            if int(getattr(widget, "admission_id", 0) or 0) != int(self.admission_id or 0):
                return None
        except Exception:
            return None
        if getattr(widget, "shift_date", None) != self._current_date:
            return None
        snapshot = getattr(widget, "_snapshot", None)
        if isinstance(snapshot, dict) and "events" in snapshot and "planned_rows" in snapshot:
            return (
                list(snapshot.get("events") or []),
                list(snapshot.get("planned_rows") or []),
            )
        if not hasattr(widget, "_events") and not hasattr(widget, "_plan"):
            return None
        return (
            list(getattr(widget, "_events", []) or []),
            getattr(widget, "_plan", None),
        )
