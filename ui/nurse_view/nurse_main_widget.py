import os
import socket
from PySide6.QtWidgets import QWidget, QHBoxLayout, QStackedWidget
from PySide6.QtCore import QTimer, Qt
from datetime import datetime, timedelta
from rem_card.app.logger import logger, log_execution_time
from rem_card.app.paths import get_role_lock_path
from rem_card.app.role_session_lock import RoleSessionLock
from rem_card.ui.shared.async_call import AsyncCallThread

ADD_PATIENT_LOCK_POLL_INTERVAL_MS = 1500
ADD_PATIENT_LOCK_KEY = "add_patient_button"
PATIENT_BED_MANAGEMENT_MODE = "patient_bed_management"
CARD_UI_PREWARM_ENABLED = os.environ.get("REMCARD_CARD_UI_PREWARM", "1") != "0"
CARD_UI_PREWARM_DELAY_MS = max(0, int(os.environ.get("REMCARD_CARD_PREWARM_DELAY_MS", "900")))
CARD_UI_PREWARM_STAGGER_MS = max(0, int(os.environ.get("REMCARD_CARD_PREWARM_STAGGER_MS", "120")))
JOURNAL_PREWARM_DELAY_MS = max(0, int(os.environ.get("REMCARD_JOURNAL_PREWARM_DELAY_MS", "60000")))
JOURNAL_PREWARM_ENABLED = os.environ.get("REMCARD_JOURNAL_PREWARM", "0") == "1"
JOURNAL_WIDGET_PREWARM_ENABLED = os.environ.get("REMCARD_JOURNAL_WIDGET_PREWARM", "0") == "1"
LOCAL_ORDER_FORCE_PREFIXES = (
    "orders_add_input:",
    "orders_left_click:",
    "orders_middle_click:",
    "orders_right_click:",
    "nurse_order_mark:",
    "nurse_order_panel_mark:",
)
ORDER_CHANGE_ENTITIES = {"orders", "administrations"}
VITALS_CACHE_CHANGE_ENTITIES = {
    "patients",
    "admissions",
    "beds",
    "operations",
    "vitals",
    "vital_settings",
    "patient_status_events",
    "fluids",
    "diet_plan",
    "oral_intake_events",
}
CARD_CACHE_CHANGE_ENTITIES = VITALS_CACHE_CHANGE_ENTITIES | ORDER_CHANGE_ENTITIES | {
    "diet_templates",
    "ivl_episodes",
    "transfusions",
    "clinical_events",
    "devices",
    "respiratory_support",
}
W1_REFRESH_ENTITIES = {
    "patients",
    "admissions",
    "beds",
    "operations",
    "vitals",
    "vital_settings",
    "patient_status_events",
    "fluids",
    "orders",
    "administrations",
    "diet_templates",
    "diet_plan",
    "oral_intake_events",
}

class NurseMainWidget(QWidget):
    """Главный виджет медсестры с изолированным UI и исправленной навигацией."""
    def __init__(self, patient_service, remcard_service, parent=None):
        super().__init__(parent)
        self.patient_service = patient_service
        self.remcard_service = remcard_service
        self._current_date = datetime.now()
        self._last_sync_time = "1970-01-01 00:00:00.000"
        self._last_change_id = 0
        self._last_global_change_id = 0
        self._update_scheduled = False
        self._last_patients_sync = datetime.now() - timedelta(minutes=1)
        self._balance_widgets_bound = False
        self._balance_quick_oral_connected = False
        self._balance_calculator_cls = None
        self._admin_signals_bound = False
        self.report_controller = None
        self._card_ui_prewarm_started = False
        self._card_ui_prewarm_done = False
        self._journal_prewarm_started = False
        self._journal_prewarm_done = False
        self._selection_mode = "beds"
        self._add_patient_lock = self._build_add_patient_lock()
        self._add_patient_lock_held = False
        self._add_patient_locked_by_other = False
        self._monitor_connected = False
        self._snapshot_worker = None
        self._snapshot_pending = None
        self._card_snapshot_cache = None
        self._balance_runtime_cache = None
        self._is_closing = False
        self.diet_intake_widget = None
        
        self.init_ui()
        self._add_patient_lock_watch_timer = QTimer(self)
        self._add_patient_lock_watch_timer.timeout.connect(self._refresh_add_patient_button_lock_state)
        self._add_patient_lock_watch_timer.start(ADD_PATIENT_LOCK_POLL_INTERVAL_MS)
        QTimer.singleShot(0, self._refresh_add_patient_button_lock_state)
        if CARD_UI_PREWARM_ENABLED:
            QTimer.singleShot(CARD_UI_PREWARM_DELAY_MS, self._schedule_card_ui_prewarm)
        if JOURNAL_PREWARM_ENABLED:
            QTimer.singleShot(JOURNAL_PREWARM_DELAY_MS, self._schedule_journal_prewarm)

    def _build_add_patient_lock(self) -> RoleSessionLock:
        owner_id = f"{socket.gethostname()}:{os.getpid()}:nurse_add_patient"
        return RoleSessionLock(
            lock_path=get_role_lock_path(ADD_PATIENT_LOCK_KEY),
            role=ADD_PATIENT_LOCK_KEY,
            owner_id=owner_id,
            stale_timeout_sec=60.0,
            heartbeat_sec=8.0,
            logger=logger,
        )

    def _acquire_add_patient_lock(self) -> bool:
        if self._add_patient_lock_held:
            return True
        if not self._add_patient_lock:
            return True
        acquired = self._add_patient_lock.acquire()
        self._add_patient_lock_held = bool(acquired)
        return self._add_patient_lock_held

    def _release_add_patient_lock(self):
        if not self._add_patient_lock_held or not self._add_patient_lock:
            return
        try:
            self._add_patient_lock.release()
        except Exception as exc:
            logger.warning("Failed to release add-patient lock (nurse): %s", exc)
        finally:
            self._add_patient_lock_held = False

    def _set_add_patient_button_hint(self, text: str):
        if hasattr(self, "sector8_panel") and hasattr(self.sector8_panel, "btn_add_patient"):
            # Tooltip intentionally disabled for this button.
            self.sector8_panel.btn_add_patient.setToolTip("")

    def _force_beds_refresh_after_journal_exit(self):
        """Локально обновляет список коек сразу после выхода из управления пациентами."""
        data_service = self._get_data_service()
        if data_service:
            try:
                data_service.request_immediate_refresh(force_emit=True)
            except Exception as exc:
                logger.warning("Failed to wake monitor after journal exit (nurse): %s", exc)

        def _refresh():
            try:
                if (
                    hasattr(self, "layout_manager")
                    and hasattr(self.layout_manager, "beds_selection_widget")
                    and self.layout_manager.beds_selection_widget
                ):
                    self.layout_manager.beds_selection_widget.refresh()
            except Exception as exc:
                logger.warning("Failed to refresh beds list after journal exit (nurse): %s", exc)

        QTimer.singleShot(0, _refresh)

    def _resolve_selection_mode(self) -> str:
        """Best-effort определение активного режима по реальному индексу стека."""
        mode = str(self._selection_mode or "")
        layout = getattr(self, "layout_manager", None)
        if not layout or not hasattr(layout, "selection_stack"):
            return mode

        stack = layout.selection_stack
        current_idx = stack.currentIndex()

        journal_idx = stack.indexOf(getattr(layout, "journal_view", None)) if hasattr(layout, "journal_view") else -1
        beds_idx = stack.indexOf(getattr(layout, "beds_view", None)) if hasattr(layout, "beds_view") else -1
        card_idx = stack.indexOf(getattr(layout, "right_area", None)) if hasattr(layout, "right_area") else -1
        archive_idx = stack.indexOf(getattr(layout, "archive_view", None)) if hasattr(layout, "archive_view") else -1
        admin_idx = stack.indexOf(getattr(layout, "admin_view", None)) if hasattr(layout, "admin_view") else -1

        if current_idx == journal_idx and journal_idx != -1:
            return PATIENT_BED_MANAGEMENT_MODE
        if current_idx == beds_idx and beds_idx != -1:
            return "beds"
        if current_idx == card_idx and card_idx != -1:
            return "card"
        if current_idx == archive_idx and archive_idx != -1:
            return "archive"
        if current_idx == admin_idx and admin_idx != -1:
            return "admin"

        return str(getattr(layout, "current_mode", mode) or mode)

    def _apply_add_patient_button_state(self):
        if not hasattr(self, "sector8_panel") or not hasattr(self.sector8_panel, "set_add_patient_enabled"):
            return
        is_beds_mode = self._selection_mode == "beds"
        enabled = is_beds_mode and not self._add_patient_locked_by_other
        self.sector8_panel.set_add_patient_enabled(enabled)

        if self._add_patient_locked_by_other:
            holder = self._add_patient_lock.describe_holder() if self._add_patient_lock else "другой пользователь"
            self._set_add_patient_button_hint(f"Добавление пациента уже открыто.\n{holder}")
        elif is_beds_mode:
            self._set_add_patient_button_hint("Открыть управление пациентами")
        else:
            self._set_add_patient_button_hint("Кнопка доступна только в режиме списка коек")

    def _refresh_add_patient_button_lock_state(self):
        resolved_mode = self._resolve_selection_mode()
        if resolved_mode:
            self._selection_mode = resolved_mode

        # Fail-safe: если уже вышли из управления пациентами, lock должен быть снят
        # даже если сигнал смены режима по какой-то причине не пришел.
        if self._selection_mode != PATIENT_BED_MANAGEMENT_MODE and self._add_patient_lock_held:
            self._release_add_patient_lock()

        locked_by_other = False
        try:
            if self._add_patient_lock and not self._add_patient_lock_held:
                locked_by_other = self._add_patient_lock.is_held_by_other()
        except Exception as exc:
            logger.warning("Failed to check add-patient lock state (nurse): %s", exc)
        self._add_patient_locked_by_other = bool(locked_by_other)
        self._apply_add_patient_button_state()

    def _get_data_service(self):
        return getattr(self.remcard_service, "data_service", None)

    def _get_read_coordinator(self):
        return getattr(self.remcard_service, "read_coordinator", None)

    def _get_cached_patient_vitals_snapshot(self, admission_id, shift_date):
        coordinator = self._get_read_coordinator()
        if coordinator is None or not admission_id or shift_date is None:
            return None
        try:
            context = coordinator.make_patient_snapshot_context(
                source_db="live",
                admission_id=int(admission_id),
                shift_date=shift_date,
                role="nurse",
                mode="live",
                variant="vitals",
            )
            if hasattr(coordinator, "get_cached_vitals"):
                return coordinator.get_cached_vitals(context.cache_key())
            if hasattr(coordinator, "get_current_cached_vitals"):
                return coordinator.get_current_cached_vitals(context.cache_key())
        except Exception as exc:
            logger.debug("Nurse vitals cache lookup failed: %s", exc)
            return None

    def _get_cached_patient_card_snapshot(self, admission_id, shift_date):
        coordinator = self._get_read_coordinator()
        if coordinator is None or not admission_id or shift_date is None:
            return None
        try:
            context = coordinator.make_patient_snapshot_context(
                source_db="live",
                admission_id=int(admission_id),
                shift_date=shift_date,
                role="nurse",
                mode="live",
                variant="card_committed",
            )
            if hasattr(coordinator, "get_cached_card"):
                return coordinator.get_cached_card(context.cache_key())
        except Exception as exc:
            logger.debug("Nurse card cache lookup failed: %s", exc)
        return None

    def _apply_patient_open_cache(self, admission_id, shift_date, snapshot):
        if not snapshot:
            return False
        load_scope = (
            "patient_open_card"
            if ("balance_runtime" in snapshot or "fluids" in snapshot)
            else "patient_open_vitals"
        )
        request = {
            "admission_id": int(admission_id),
            "shift_date": shift_date,
            "ensure_initial_status": False,
            "load_scope": load_scope,
            "context_key": self._current_snapshot_context_key(
                admission_id=admission_id,
                shift_date=shift_date,
                load_scope=load_scope,
            ),
            "snapshot": snapshot,
            "from_cache": True,
        }
        self._apply_card_snapshot(request)
        logger.info(
            "NurseMainWidget applied cached vitals snapshot admission_id=%s version=%s",
            admission_id,
            snapshot.get("version"),
        )
        return True

    def _chart_matches_context(self, admission_id, start_dt):
        chart = getattr(self, "chart", None)
        if chart is None:
            return False
        return (
            int(getattr(chart, "admission_id", 0) or 0) == int(admission_id or 0)
            and getattr(chart, "start_time", None) == start_dt
            and bool(getattr(chart, "vitals_data", None))
        )

    def _ensure_diet_widget(self):
        if getattr(self, "diet_intake_widget", None) is not None:
            return self.diet_intake_widget
        if not hasattr(self, "layout_manager") or not hasattr(self.layout_manager, "sector_5"):
            return None
        from rem_card.ui.shared.components.diet_intake_widget import DietIntakeWidget

        self.diet_intake_widget = DietIntakeWidget(self.remcard_service, role="nurse", show_prn_input=False)
        self.diet_intake_widget.data_changed.connect(self._update_balance_calculations)
        self.layout_manager.sector_5.set_content(self.diet_intake_widget)
        return self.diet_intake_widget

    def _configure_balance_quick_oral_input(self):
        sector_2b_g = getattr(getattr(self, "layout_manager", None), "sector_2b_g", None)
        if sector_2b_g is None or not hasattr(sector_2b_g, "configure_quick_oral_intake"):
            return

        admission_id = getattr(self.layout_manager, "current_admission_id", None)
        sector_2b_g.configure_quick_oral_intake(
            service=self.remcard_service,
            admission_id=admission_id,
            shift_date=self._current_date,
            visible=bool(admission_id),
        )
        if not self._balance_quick_oral_connected and hasattr(sector_2b_g, "oral_intake_changed"):
            sector_2b_g.oral_intake_changed.connect(self._on_balance_quick_oral_changed)
            self._balance_quick_oral_connected = True

    def _on_balance_quick_oral_changed(self):
        diet_widget = self._ensure_diet_widget()
        if diet_widget:
            diet_widget.refresh_data()
        self._update_balance_calculations()

    def _current_snapshot_context_key(
        self,
        *,
        admission_id=None,
        shift_date=None,
        load_scope: str = "full",
    ):
        target_admission_id = int(
            admission_id
            if admission_id is not None
            else (getattr(self.layout_manager, "current_admission_id", None) or 0)
        )
        target_shift_date = shift_date if shift_date is not None else self._current_date
        coordinator = self._get_read_coordinator()
        if coordinator is not None and target_shift_date is not None:
            try:
                context = coordinator.make_patient_snapshot_context(
                    source_db="live",
                    admission_id=target_admission_id,
                    shift_date=target_shift_date,
                    role="nurse",
                    mode="live",
                    variant=str(load_scope or "full"),
                )
                context_hash = context.hash()
            except Exception:
                context_hash = "unavailable"
        else:
            context_hash = "unavailable"
        return (
            target_admission_id,
            target_shift_date.isoformat() if target_shift_date else None,
            "nurse",
            "live",
            str(load_scope or "full"),
            context_hash,
        )

    def _ensure_monitor_subscription(self):
        data_service = self._get_data_service()
        if not data_service or self._monitor_connected:
            return
        data_service.changes_detected.connect(self._on_data_changes, Qt.QueuedConnection)
        self._monitor_connected = True

    def _disconnect_monitor(self):
        data_service = self._get_data_service()
        if not data_service or not self._monitor_connected:
            return
        try:
            data_service.changes_detected.disconnect(self._on_data_changes)
        except Exception:
            pass
        self._monitor_connected = False

    def _request_card_snapshot(
        self,
        *,
        ensure_initial_status: bool = False,
        force_emit: bool = False,
        load_scope: str = "full",
    ):
        adm_id = getattr(self.layout_manager, "current_admission_id", None)
        if not adm_id:
            return

        request = {
            "admission_id": int(adm_id),
            "shift_date": self._current_date,
            "ensure_initial_status": bool(ensure_initial_status),
            "load_scope": str(load_scope or "full"),
            "context_key": self._current_snapshot_context_key(load_scope=load_scope),
        }
        if self._snapshot_worker and self._snapshot_worker.isRunning():
            self._snapshot_pending = request
            return

        worker = AsyncCallThread(self._build_card_snapshot_job, request, parent=self)
        self._snapshot_worker = worker
        worker.succeeded.connect(self._apply_card_snapshot)
        worker.failed.connect(self._on_card_snapshot_failed)
        worker.finished.connect(lambda: self._on_card_snapshot_finished(worker))
        worker.start()

        data_service = self._get_data_service()
        if data_service:
            data_service.request_immediate_refresh(force_emit=force_emit)

    def _schedule_balance_prefetch(self, admission_id: int, shift_date: datetime):
        context_key = self._current_snapshot_context_key(
            admission_id=admission_id,
            shift_date=shift_date,
            load_scope="full",
        )
        QTimer.singleShot(
            0,
            lambda: self._prefetch_balance_if_current(admission_id, shift_date, context_key),
        )

    def _prefetch_balance_if_current(self, admission_id: int, shift_date: datetime, context_key):
        current_admission_id = getattr(self.layout_manager, "current_admission_id", None)
        if int(admission_id or 0) != int(current_admission_id or 0):
            return
        if shift_date != self._current_date:
            return
        if context_key != self._current_snapshot_context_key(load_scope="full"):
            return

        snapshot = self._card_snapshot_cache or {}
        if "fluids" in snapshot and "balance_runtime" in snapshot:
            return

        self._request_card_snapshot(load_scope="full")

    def _build_card_snapshot_job(self, request: dict):
        load_scope = str(request.get("load_scope") or "full")
        if load_scope == "patient_open_vitals":
            coordinator = self._get_read_coordinator()
            if coordinator is not None:
                snapshot = coordinator.load_patient_vitals_snapshot(
                    request["admission_id"],
                    request["shift_date"],
                    role="nurse",
                    mode="live",
                    source_db="live",
                    ensure_initial_status=request["ensure_initial_status"],
                    force_refresh=False,
                )
            else:
                logger.warning(
                    "NurseMainWidget: ReadCoordinator unavailable, using build_full_card_snapshot for patient open"
                )
                snapshot = self.remcard_service.build_full_card_snapshot(
                    request["admission_id"],
                    request["shift_date"],
                    include_change_cursor=True,
                    include_balance=True,
                    balance_only_committed=True,
                    ensure_initial_status=request["ensure_initial_status"],
                )
        elif load_scope in {"patient_open_card", "full"}:
            coordinator = self._get_read_coordinator()
            if coordinator is not None and hasattr(coordinator, "load_patient_card_snapshot"):
                snapshot = coordinator.load_patient_card_snapshot(
                    request["admission_id"],
                    request["shift_date"],
                    role="nurse",
                    mode="live",
                    source_db="live",
                    ensure_initial_status=request["ensure_initial_status"],
                    balance_only_committed=True,
                    force_refresh=False,
                )
            else:
                snapshot = self.remcard_service.build_full_card_snapshot(
                    request["admission_id"],
                    request["shift_date"],
                    include_change_cursor=True,
                    include_balance=True,
                    balance_only_committed=True,
                    ensure_initial_status=request["ensure_initial_status"],
                )
        else:
            snapshot = self.remcard_service.build_full_card_snapshot(
                request["admission_id"],
                request["shift_date"],
                include_change_cursor=True,
                include_balance=True,
                balance_only_committed=True,
                ensure_initial_status=request["ensure_initial_status"],
            )
        request["snapshot"] = snapshot
        return request

    def _apply_card_snapshot(self, request: dict):
        adm_id = getattr(self.layout_manager, "current_admission_id", None)
        if int(request["admission_id"]) != int(adm_id or 0) or request["shift_date"] != self._current_date:
            return
        if request.get("context_key") != self._current_snapshot_context_key(
            load_scope=request.get("load_scope", "full")
        ):
            logger.info(
                "NurseMainWidget discarded stale snapshot admission_id=%s load_scope=%s request_context=%s current_context=%s",
                request.get("admission_id"),
                request.get("load_scope"),
                request.get("context_key"),
                self._current_snapshot_context_key(load_scope=request.get("load_scope", "full")),
            )
            return

        snapshot = dict(request.get("snapshot") or {})
        previous_snapshot = self._card_snapshot_cache or {}
        if (
            previous_snapshot
            and not request.get("from_cache")
            and previous_snapshot.get("cache_key") == snapshot.get("cache_key")
            and int(previous_snapshot.get("version") or 0) == int(snapshot.get("version") or 0)
            and previous_snapshot.get("scope") == snapshot.get("scope")
            and previous_snapshot.get("load_trace_id") == snapshot.get("load_trace_id")
        ):
            logger.info(
                "NurseMainWidget skipped unchanged cached snapshot admission_id=%s scope=%s version=%s",
                request.get("admission_id"),
                snapshot.get("scope"),
                snapshot.get("version"),
            )
            return
        self._card_snapshot_cache = snapshot
        self._balance_runtime_cache = snapshot.get("balance_runtime")
        effective_bounds = snapshot.get("effective_bounds")
        chart_active_intervals = snapshot.get("chart_active_intervals")

        self._ensure_card_widgets_initialized()
        self._bind_balance_widgets_if_ready()

        if hasattr(self, "chart"):
            runtime = snapshot.get("balance_runtime") or {}
            self.chart.update_data(
                snapshot.get("vitals_extended") or [],
                snapshot.get("start_dt"),
                active_intervals=chart_active_intervals or runtime.get("active_intervals"),
            )

        if hasattr(self, "vitals_input") and effective_bounds:
            self.vitals_input.admission_id = adm_id
            self.vitals_input.shift_date = self._current_date
            self.vitals_input.apply_context_snapshot(
                patient=snapshot.get("patient"),
                settings=snapshot.get("settings") or {},
                effective_bounds=effective_bounds,
                has_vitals=bool(snapshot.get("has_vitals")),
            )

        if hasattr(self, "balance_controller") and effective_bounds and snapshot.get("fluids") is not None:
            self.balance_controller.apply_loaded_data(
                snapshot.get("fluids") or [],
                effective_bounds,
            )

        self.refresh_data()
        self._last_change_id = max(
            int(self._last_change_id or 0),
            int(snapshot.get("change_id") or 0),
        )

    def _on_card_snapshot_failed(self, exc: Exception):
        logger.error("NurseMainWidget snapshot load failed: %s", exc, exc_info=True)

    def _on_card_snapshot_finished(self, worker):
        if self._snapshot_worker is worker:
            self._snapshot_worker = None
        if self._snapshot_pending:
            pending = self._snapshot_pending
            self._snapshot_pending = None
            QTimer.singleShot(
                0,
                lambda req=pending: self._request_card_snapshot(
                    ensure_initial_status=req["ensure_initial_status"],
                    load_scope=req.get("load_scope", "full"),
                ),
            )

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
        adm_id = getattr(self.layout_manager, "current_admission_id", None)
        if not adm_id:
            return False
        if payload.get("forced"):
            return True
        relevant_entities = {"patients", "admissions", "beds", "operations", "diet_templates"}
        orders_entities = {"orders", "administrations"}
        for change in payload.get("changes") or []:
            admission_id = change.get("admission_id")
            entity_name = str(change.get("entity_name") or "")
            if admission_id is not None and int(admission_id) == int(adm_id):
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

    def _invalidate_vitals_cache_from_payload(self, payload: dict, changed_entities: set[str]) -> None:
        force_sources = self._payload_force_sources(payload)
        vitals_entities = changed_entities.intersection(VITALS_CACHE_CHANGE_ENTITIES)
        card_entities = changed_entities.intersection(CARD_CACHE_CHANGE_ENTITIES)
        has_relevant_entities = bool(vitals_entities or card_entities)
        has_forced_source = bool(payload.get("forced") and force_sources)
        if not (has_relevant_entities or has_forced_source):
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
            if entity_name in CARD_CACHE_CHANGE_ENTITIES and admission_id is not None:
                admission_ids.add(int(admission_id))

        if not admission_ids and (has_relevant_entities or has_forced_source):
            current_admission_id = getattr(self.layout_manager, "current_admission_id", None)
            if current_admission_id:
                admission_ids.add(int(current_admission_id))

        for admission_id in admission_ids:
            logger.info(
                "NurseMainWidget preserves patient snapshot cache for stale-while-revalidate admission_id=%s reason=%s",
                admission_id,
                f"data_changes:{','.join(sorted(changed_entities)) or ','.join(force_sources) or 'forced'}",
            )

    def _on_data_changes(self, payload: dict):
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
        self._invalidate_vitals_cache_from_payload(payload, changed_entities)
        orders_entities = {"orders", "administrations"}
        if self._selection_mode == "beds" and (payload.get("forced") or changed_entities.intersection(W1_REFRESH_ENTITIES)):
            if hasattr(self.layout_manager, "beds_selection_widget") and self.layout_manager.beds_selection_widget:
                self.layout_manager.beds_selection_widget.refresh()
        if self._selection_mode == "archive":
            archive_widget = getattr(self.layout_manager, "archive_widget", None)
            if archive_widget and (payload.get("forced") or changed_entities.intersection({"patients", "admissions"})):
                archive_widget.load_data()

        if self._selection_mode != "card" or not self._payload_is_relevant(payload):
            return

        diet_entities = {"diet_templates", "diet_plan", "oral_intake_events"}
        if self._is_local_orders_force_payload(payload, changed_entities):
            if hasattr(self.layout_manager, 'orders_widget'):
                try:
                    self.layout_manager.orders_widget.handle_data_changes(
                        payload,
                        tab_active=self._is_orders_tab_active(),
                    )
                except Exception:
                    logger.exception("Nurse orders local forced skip failed")
            logger.info(
                "[OrdersClick] skip local forced card snapshot role=nurse admission_id=%s sources=%s entities=%s",
                getattr(self.layout_manager, "current_admission_id", None),
                self._payload_force_sources(payload),
                sorted(changed_entities),
            )
            return

        has_diet_changes = bool(changed_entities.intersection(diet_entities))
        if payload.get("forced") and getattr(self, "diet_intake_widget", None):
            self.diet_intake_widget.handle_data_changes(payload)
        if has_diet_changes and getattr(self, "diet_intake_widget", None):
            self.diet_intake_widget.handle_data_changes(payload)
            if "oral_intake_events" in changed_entities:
                self._update_balance_calculations()
            if not payload.get("forced") and set(changed_entities).issubset(diet_entities):
                return

        has_orders_changes = bool(changed_entities.intersection(orders_entities))
        if (payload.get("forced") or has_orders_changes) and hasattr(self.layout_manager, 'orders_widget'):
            try:
                self.layout_manager.orders_widget.handle_data_changes(
                    payload,
                    tab_active=self._is_orders_tab_active(),
                )
            except Exception:
                logger.exception("Nurse orders delta refresh failed")
        if (payload.get("forced") or has_orders_changes) and hasattr(self.layout_manager, "nurse_orders_manager"):
            try:
                mgr = self.layout_manager.nurse_orders_manager
                if mgr and hasattr(mgr, "handle_data_changes"):
                    mgr.handle_data_changes(payload)
            except Exception:
                logger.exception("Current nurse orders refresh failed")
        self._request_card_snapshot()

    def _is_orders_tab_active(self) -> bool:
        return (
            hasattr(self.layout_manager, 'vitals_stack')
            and self.layout_manager.vitals_stack.currentIndex() == 1
            and hasattr(self.layout_manager, 'orders_widget')
        )

    def start_auto_refresh(self):
        self._ensure_monitor_subscription()
        if hasattr(self.layout_manager, "beds_selection_widget") and self.layout_manager.beds_selection_widget:
            self.layout_manager.beds_selection_widget.refresh(queue_if_running=False)
        data_service = self._get_data_service()
        if data_service:
            data_service.request_immediate_refresh(force_emit=False)

    def stop_auto_refresh(self):
        self._disconnect_monitor()

    def init_ui(self):
        from .nurse_remcard_layout import NurseRemCardLayoutManager
        from .components.nurse_sector8_panel import NurseSector8Panel

        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        
        self.main_stack = QStackedWidget()
        
        # Используем ИЗОЛИРОВАННЫЙ лайаут медсестры
        self.layout_manager = NurseRemCardLayoutManager(patient_service=self.patient_service,
                                                      remcard_service=self.remcard_service)
        self.layout_manager.patient_status_service = self.remcard_service.status_service
        
        self.main_stack.addWidget(self.layout_manager)
        main_layout.addWidget(self.main_stack)
        
        # Настройка сигналов медсестринского сектора 4в
        if hasattr(self.layout_manager, 'sector_4v'):
            self.layout_manager.sector_4v.show_card_requested.connect(lambda: self.load_patient_card(self.layout_manager.current_admission_id, datetime.now()))
            self.layout_manager.sector_4v.yest_card_requested.connect(self.on_yest_card_clicked)
            self.layout_manager.sector_4v.full_report_requested.connect(self.on_full_report_clicked)
            self.layout_manager.sector_4v.daily_report_requested.connect(self.on_daily_report_clicked)
            self.layout_manager.sector_4v.archive_requested.connect(self.show_archive)

        # Панель управления медсестры (Сектор 8)
        self.sector8_panel = NurseSector8Panel()
        self.sector8_panel.btn_back.clicked.connect(self.on_back_clicked)
        self.sector8_panel.btn_exit.clicked.connect(self.on_exit_clicked)
        self.sector8_panel.refresh_clicked.connect(self.force_refresh_everywhere)
        self.sector8_panel.add_patient_clicked.connect(self.on_add_patient_clicked)
        self.sector8_panel.bonus_clicked.connect(self.on_bonus_clicked)
        
        self.layout_manager.sector_8.set_content(self.sector8_panel)
        self._ensure_diet_widget()
        if hasattr(self.layout_manager, "selection_mode_changed"):
            self.layout_manager.selection_mode_changed.connect(self._on_selection_mode_changed)
            self._on_selection_mode_changed(getattr(self.layout_manager, "current_mode", "beds"))
        if hasattr(self.layout_manager, "sector_2b"):
            self.layout_manager.sector_2b.tab_changed.connect(self.on_tab_changed)

        # Инициализация событий (аналогично врачу), без раннего создания тяжелой вкладки.
        if hasattr(self.layout_manager, "register_events_status_handler"):
            self.layout_manager.register_events_status_handler(self.refresh_data)
        elif hasattr(self.layout_manager, 'sector_events') and self.layout_manager.sector_events:
            self.layout_manager.sector_events.role = "Медсестра"
            self.layout_manager.sector_events.status_changed.connect(self.refresh_data)

        # Подключаем выбор пациента из списка коек
        self.layout_manager.beds_selection_widget.patient_selected.connect(self.on_patient_selected)

    def _schedule_card_ui_prewarm(self):
        if self._card_ui_prewarm_started or self._card_ui_prewarm_done:
            return
        self._card_ui_prewarm_started = True
        QTimer.singleShot(0, self._run_card_ui_prewarm)

    def _run_card_ui_prewarm(self):
        if self._card_ui_prewarm_done:
            return
        try:
            if hasattr(self, 'layout_manager'):
                self.layout_manager.setUpdatesEnabled(False)
            self._ensure_card_widgets_initialized()
            QTimer.singleShot(CARD_UI_PREWARM_STAGGER_MS, self._run_card_ui_prewarm_stage_2)
        except Exception as exc:
            logger.warning("Nurse card UI prewarm stage1 failed: %s", exc)
            self._card_ui_prewarm_started = False
        finally:
            if hasattr(self, 'layout_manager'):
                self.layout_manager.setUpdatesEnabled(True)

    def _run_card_ui_prewarm_stage_2(self):
        if self._card_ui_prewarm_done:
            return
        try:
            if hasattr(self.layout_manager, 'orders_widget'):
                ow = self.layout_manager.orders_widget
                if getattr(ow, "main_layout", None) is None:
                    ow.setup_ui()
            QTimer.singleShot(CARD_UI_PREWARM_STAGGER_MS, self._run_card_ui_prewarm_stage_3)
        except Exception as exc:
            logger.warning("Nurse card UI prewarm stage2 failed: %s", exc)
            self._card_ui_prewarm_started = False

    def _run_card_ui_prewarm_stage_3(self):
        if self._card_ui_prewarm_done:
            return
        try:
            if hasattr(self.layout_manager, 'ensure_nurse_orders_manager'):
                self.layout_manager.ensure_nurse_orders_manager()
            self._card_ui_prewarm_done = True
            logger.debug("Nurse card UI prewarm completed")
        except Exception as exc:
            logger.warning("Nurse card UI prewarm stage3 failed: %s", exc)
            self._card_ui_prewarm_started = False

    def _schedule_journal_prewarm(self):
        if self._journal_prewarm_started or self._journal_prewarm_done:
            return
        self._journal_prewarm_started = True
        QTimer.singleShot(0, self._run_journal_prewarm)

    def _run_journal_prewarm(self):
        if self._journal_prewarm_done:
            return

        try:
            if not JOURNAL_WIDGET_PREWARM_ENABLED:
                self._journal_prewarm_done = True
                return

            if hasattr(self, "layout_manager") and hasattr(self.layout_manager, "prewarm_journal_widget"):
                self.layout_manager.prewarm_journal_widget()
                self._journal_prewarm_done = True
                logger.debug("Nurse patient-bed management widget prewarm completed")
        except Exception as exc:
            logger.warning("Nurse patient-bed management prewarm failed: %s", exc)
        finally:
            if not self._journal_prewarm_done:
                self._journal_prewarm_started = False

    def _ensure_card_widgets_initialized(self):
        if hasattr(self, "chart") and hasattr(self, "vitals_input") and hasattr(self, "balance_controller"):
            return

        from ..shared.chart_widget import ChartWidget
        from ..shared.vitals_widget import VitalsWidget
        from ..shared.components.balance_controller import BalanceController

        self.chart = ChartWidget()
        self.chart.service = self.remcard_service
        self.chart.status_service = self.remcard_service.status_service
        self.layout_manager.sector_2v.set_content(self.chart)

        self.vitals_input = VitalsWidget(self.remcard_service, None, datetime.now())
        self.vitals_input.save_btn.clicked.connect(self.refresh_data)
        self.vitals_input.data_changed.connect(self.refresh_data)
        self.layout_manager.sector_1b.set_content(self.vitals_input)

        self.balance_controller = BalanceController(self.remcard_service._fluids, None, self._current_date)
        self._bind_balance_widgets_if_ready()

    def _bind_balance_widgets_if_ready(self) -> bool:
        if not hasattr(self, "balance_controller") or self.balance_controller is None:
            return False
        if self._balance_widgets_bound:
            return True

        lm = getattr(self, "layout_manager", None)
        if lm is None:
            return False

        grid = getattr(lm, "balance_grid", None)
        panel = getattr(lm, "sector_2d", None)
        quick = getattr(lm, "sector_2b_v", None)
        summary = getattr(lm, "sector_3b", None)
        if not (grid and panel and quick and summary):
            return False

        self.balance_controller.set_widgets(
            grid,
            panel,
            [quick, summary],
        )
        self.balance_controller.data_updated.connect(self.refresh_data)
        self._balance_widgets_bound = True
        return True

    def _apply_balance_snapshot_if_available(self) -> bool:
        snapshot = self._card_snapshot_cache or {}
        if not hasattr(self, "balance_controller") or self.balance_controller is None:
            return False
        effective_bounds = snapshot.get("effective_bounds")
        if not effective_bounds:
            return False
        if "fluids" not in snapshot or "balance_runtime" not in snapshot:
            return False
        self.balance_controller.apply_loaded_data(
            snapshot.get("fluids") or [],
            effective_bounds,
        )
        return True

    def _ensure_balance_tab_ready(self):
        if hasattr(self.layout_manager, "ensure_balance_tab_initialized"):
            self.layout_manager.ensure_balance_tab_initialized()
        self._configure_balance_quick_oral_input()
        if not self._bind_balance_widgets_if_ready():
            return
        if hasattr(self, "balance_controller") and self.balance_controller:
            self.balance_controller.admission_id = getattr(self.layout_manager, "current_admission_id", None)
            self.balance_controller.shift_date = self._current_date
        if not self._apply_balance_snapshot_if_available():
            self._request_card_snapshot(load_scope="full")

    @property
    def current_date(self):
        return self._current_date

    def _get_report_controller(self):
        if self.report_controller is None:
            from ..shared.report_controller import RemCardReportController

            self.report_controller = RemCardReportController(self.remcard_service, self)
        return self.report_controller

    @current_date.setter
    def current_date(self, value):
        self._current_date = value
        if hasattr(self.layout_manager, 'sector_2a'):
            start_dt, _ = self.remcard_service.get_day_period(value)
            self.layout_manager.sector_2a.update_period(start_dt)
        diet_widget = self._ensure_diet_widget()
        if diet_widget and getattr(self.layout_manager, 'current_admission_id', None):
            diet_widget.set_context(self.layout_manager.current_admission_id, self._current_date)
        self._configure_balance_quick_oral_input()
        # Держим сектор 5 в синхроне с датой открытой карты.
        if (
            hasattr(self, 'layout_manager')
            and hasattr(self.layout_manager, 'nurse_orders_manager')
            and getattr(self.layout_manager, 'current_admission_id', None)
        ):
            mgr = self.layout_manager.nurse_orders_manager
            if mgr:
                mgr.set_context(
                    self.layout_manager.current_admission_id,
                    self._current_date
                )

    def load_patient_card(self, admission_id, date):
        self._schedule_card_ui_prewarm()
        self._ensure_card_widgets_initialized()
        self.layout_manager.current_admission_id = admission_id
        self.current_date = date
        self._card_snapshot_cache = None
        self._balance_runtime_cache = None
        
        # Рассчитываем мед. сутки для правильной инициализации
        start_dt, end_dt = self.remcard_service.get_day_period(date)
        cached_card_snapshot = self._get_cached_patient_card_snapshot(admission_id, date)
        cached_vitals_snapshot = cached_card_snapshot or self._get_cached_patient_vitals_snapshot(admission_id, date)
        
        # Обновляем контекст сектора событий (без принудительного раннего создания вкладки).
        if hasattr(self.layout_manager, "set_events_context"):
            self.layout_manager.set_events_context(
                admission_id=admission_id,
                status_service=self.remcard_service.status_service,
                shift_date=date,
                shift_start=start_dt,
                shift_end=end_dt,
            )

        self.chart.admission_id = admission_id
        if (
            hasattr(self.chart, "clear_for_context")
            and not cached_vitals_snapshot
            and not self._chart_matches_context(admission_id, start_dt)
        ):
            self.chart.clear_for_context(admission_id=admission_id, start_time=start_dt)
        self.vitals_input.admission_id = admission_id
        self.vitals_input.shift_date = date
        self.vitals_input.mark_dirty()

        if hasattr(self, 'balance_controller'):
            self.balance_controller.admission_id = admission_id
            self.balance_controller.shift_date = date

        diet_widget = self._ensure_diet_widget()
        if diet_widget:
            diet_widget.set_context(admission_id, date)
        self._configure_balance_quick_oral_input()

        # Обновляем orders_widget
        if hasattr(self.layout_manager, "orders_widget") and self.layout_manager.orders_widget:
            ow = self.layout_manager.orders_widget
            if hasattr(ow, "set_context"):
                ow.set_context(
                    service=self.remcard_service,
                    admission_id=admission_id,
                    shift_date=date,
                )
            else:
                ow.service = self.remcard_service
                ow.admission_id = admission_id
                ow.shift_date = date

        # Обновляем контекст 1а/5 явно: важно и для ПЕРВОГО входа в карту.
        # Manager может отсутствовать до первого переключения из режима коек в режим карты.
        nurse_orders_mgr = None
        if hasattr(self.layout_manager, "ensure_nurse_orders_manager"):
            nurse_orders_mgr = self.layout_manager.ensure_nurse_orders_manager()
        elif hasattr(self.layout_manager, "nurse_orders_manager"):
            nurse_orders_mgr = self.layout_manager.nurse_orders_manager
        if nurse_orders_mgr:
            QTimer.singleShot(0, lambda mgr=nurse_orders_mgr, aid=admission_id, d=date: mgr.set_context(aid, d))

        self._last_change_id = 0
        if not cached_card_snapshot:
            self._reset_balance_view_state()
        if cached_vitals_snapshot:
            self._apply_patient_open_cache(admission_id, date, cached_vitals_snapshot)
        if cached_vitals_snapshot:
            self._request_card_snapshot(
                ensure_initial_status=True,
                load_scope="patient_open_card",
            )
        else:
            self._request_card_snapshot(
                ensure_initial_status=True,
                load_scope="patient_open_vitals",
            )
            self._schedule_balance_prefetch(admission_id, date)
        if hasattr(self, 'layout_manager'):
            self.layout_manager.set_active_tab("Витальные функции")
            if hasattr(self.layout_manager, 'sector_2b'):
                self.layout_manager.sector_2b.on_tab_clicked("Витальные функции")

    def _prime_patient_header_from_w1(self, patient, target_date):
        """Заполняет 4б/4в данными W1 до показа карты, чтобы не было визуального скачка."""
        if not patient or not hasattr(self, "layout_manager"):
            return
        layout = self.layout_manager
        runtime = dict(getattr(patient, "_w1_runtime_snapshot", None) or {})
        try:
            if hasattr(layout, "sector_4b"):
                layout.sector_4b.update_patient_info(patient, target_date)

                if "status" in runtime:
                    status_dto = runtime.get("status")
                    if hasattr(layout, "set_current_status_dto"):
                        layout.set_current_status_dto(status_dto)
                    else:
                        layout.sector_4b.update_status(status_dto)
                    if hasattr(layout.sector_4b, "update_outcome_timer"):
                        layout.sector_4b.update_outcome_timer(
                            status_dto,
                            int(runtime.get("outcome_delay_min") or 30),
                        )

            if hasattr(layout, "sector_4v"):
                latest_values = runtime.get("latest_values")
                settings = runtime.get("settings")
                if latest_values is not None or settings is not None:
                    layout.sector_4v.update_latest_vitals(latest_values or {}, settings)

                runtime_now = runtime.get("now")
                same_shift = False
                if runtime_now is not None:
                    try:
                        target_start, _ = self.remcard_service.get_day_period(target_date)
                        runtime_start, _ = self.remcard_service.get_day_period(runtime_now)
                        same_shift = target_start == runtime_start
                    except Exception:
                        same_shift = False

                if same_shift and ("card_exists" in runtime or "yest_exists" in runtime):
                    layout.sector_4v.set_buttons_state(
                        bool(runtime.get("card_exists")),
                        bool(runtime.get("yest_exists")),
                    )

            logger.info(
                "[NURSE_VIEW] primed patient header from W1 admission_id=%s has_runtime=%s",
                getattr(patient, "id", None),
                int(bool(runtime)),
            )
        except Exception as exc:
            logger.warning("Failed to prime nurse patient header from W1: %s", exc, exc_info=True)

    def refresh_data(self):
        adm_id = self.layout_manager.current_admission_id
        if not adm_id: return
        snapshot = self._card_snapshot_cache or {}
        if hasattr(self, 'layout_manager') and hasattr(self.layout_manager, 'sector_4b'):
            patient = snapshot.get("patient")
            if patient:
                self.layout_manager.sector_4b.update_patient_info(patient, self.current_date)
        if hasattr(self.layout_manager, 'sector_4v'):
            latest_values = snapshot.get("latest_values") or {}
            settings = snapshot.get("settings") or {}
            if hasattr(self.layout_manager, 'sector_2g'):
                self.layout_manager.sector_2g.update_legend(settings)
            self.layout_manager.sector_4v.update_latest_vitals(latest_values, settings)
            self.layout_manager.sector_4v.set_buttons_state(
                bool(snapshot.get("card_exists")),
                bool(snapshot.get("yest_exists")),
            )
        if hasattr(self.layout_manager, "set_current_status_dto"):
            self.layout_manager.set_current_status_dto(snapshot.get("status"))
        self.layout_manager.refresh_current_status()
        self._bind_balance_widgets_if_ready()
        self._update_balance_calculations()

    def on_tab_changed(self, tab_name):
        self.layout_manager.set_active_tab(tab_name)
        if tab_name == "Баланс жидкости":
            self._ensure_balance_tab_ready()
        elif tab_name == "Назначения":
            ow = self.layout_manager.orders_widget
            if hasattr(ow, "set_context"):
                ow.set_context(
                    service=self.remcard_service,
                    admission_id=self.layout_manager.current_admission_id,
                    shift_date=self._current_date,
                )
            else:
                ow.service = self.remcard_service
                ow.admission_id = self.layout_manager.current_admission_id
                ow.shift_date = self._current_date
            ow.ensure_ready_for_show()

    def on_patient_selected(self, patient, action_type):
        if action_type == "show":
            target_date = datetime.now()
            self.load_patient_card(patient.id, target_date)
            self._prime_patient_header_from_w1(patient, target_date)
            self.layout_manager.set_patient_selection_mode("card")
        elif action_type == "yest":
            yest_date = datetime.now() - timedelta(days=1)
            self.load_patient_card(patient.id, yest_date)
            self._prime_patient_header_from_w1(patient, yest_date)
            self.layout_manager.set_patient_selection_mode("card")
        elif action_type == "archive":
            self.show_archive(patient)

    def show_archive(self, patient=None):
        from rem_card.ui.shared.custom_message_box import CustomMessageBox
        from ..shared.patient_archive_dialog import PatientArchiveDialog
        from PySide6.QtWidgets import QDialog
        
        if not patient:
            adm_id = self.layout_manager.current_admission_id
            if not adm_id: return
            patient = self.remcard_service.get_patient(adm_id)
            
        if not patient:
            CustomMessageBox.warning(self, "Ошибка", "Пациент не найден.")
            return
            
        dialog = PatientArchiveDialog(self.remcard_service, patient, self)
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        result = dialog.exec()
        
        if result == QDialog.Accepted:
            selected_date = dialog.get_selected_date()
            if selected_date:
                target_dt = datetime.fromtimestamp(selected_date.timestamp())
                # Загружаем карту и переключаемся в режим карты
                self.load_patient_card(patient.id, target_dt)
                self.layout_manager.set_patient_selection_mode("card")

    def on_yest_card_clicked(self):
        yest_date = self.current_date - timedelta(days=1)
        self.load_patient_card(self.layout_manager.current_admission_id, yest_date)

    def on_daily_report_clicked(self):
        """Обработка запроса отчета за сутки из открытой карты пациента (медсестра)."""
        adm_id = self.layout_manager.current_admission_id
        controller = self._get_report_controller()
        controller.run_daily_report(adm_id, self.current_date)
        self.daily_worker = controller.daily_worker

    def on_full_report_clicked(self):
        """Обработка запроса общего отчета из открытой карты пациента (медсестра)."""
        adm_id = self.layout_manager.current_admission_id
        controller = self._get_report_controller()
        controller.run_full_report(adm_id)
        self.report_worker = controller.full_worker

    @log_execution_time(threshold_ms=50)
    def on_calculator_clicked(self):
        from rem_card.ui.shared.components.infusion_calculator import InfusionCalculatorDialog

        dialog = InfusionCalculatorDialog(parent=self)
        dialog.exec()

    @log_execution_time(threshold_ms=50)
    def auto_refresh(self, force=False):
        if hasattr(self, 'vitals_input'):
            self.vitals_input.refresh_time_only()
        self._update_balance_calculations()
        data_service = self._get_data_service()
        if force:
            self.force_refresh_everywhere()
        if data_service:
            data_service.request_immediate_refresh(force_emit=force)

    def force_refresh_everywhere(self):
        """Принудительное обновление всех доступных представлений (кнопка 'Обновить')."""
        data_service = self._get_data_service()
        if data_service:
            data_service.request_immediate_refresh(force_emit=True)

        try:
            if hasattr(self.layout_manager, 'beds_selection_widget') and self.layout_manager.beds_selection_widget:
                self.layout_manager.beds_selection_widget.refresh()
        except Exception:
            pass

        current_idx = self.layout_manager.selection_stack.currentIndex() if hasattr(self.layout_manager, "selection_stack") else -1
        is_card_mode = current_idx == 0 and bool(getattr(self.layout_manager, "current_admission_id", None))

        if is_card_mode:
            try:
                self._request_card_snapshot(ensure_initial_status=True, force_emit=True)
                if hasattr(self.layout_manager, 'orders_widget') and self.layout_manager.orders_widget:
                    self.layout_manager.orders_widget.request_refresh(force=True)
                if hasattr(self.layout_manager, 'nurse_orders_manager') and self.layout_manager.nurse_orders_manager:
                    self.layout_manager.nurse_orders_manager.refresh_data()
                events_sector = None
                if hasattr(self.layout_manager, "ensure_events_sector"):
                    events_sector = self.layout_manager.ensure_events_sector()
                else:
                    events_sector = getattr(self.layout_manager, "sector_events", None)
                if events_sector:
                    events_sector.refresh()
            except Exception as exc:
                logger.warning("Nurse force refresh: card mode refresh failed: %s", exc, exc_info=True)

        try:
            if hasattr(self.layout_manager, "journal_widget") and self.layout_manager.journal_widget:
                jw = self.layout_manager.journal_widget
                if hasattr(jw, "refresh_data"):
                    jw.refresh_data()
                if hasattr(jw, "refresh_bed_statuses"):
                    jw.refresh_bed_statuses()
        except Exception as exc:
            logger.warning("Nurse force refresh: journal refresh failed: %s", exc)

    def perform_coalesced_update(self):
        self._update_scheduled = False
        self._request_card_snapshot()

    def on_exit_clicked(self):
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        reply = CustomMessageBox.question(self, "Подтверждение", "Выйти из программы?", CustomMessageBox.Yes | CustomMessageBox.No, CustomMessageBox.No)
        if reply == CustomMessageBox.Yes: self.window().close()

    def on_bonus_clicked(self):
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        try:
            from rem_card.app.runtime_paths import resolve_baza_dir
            from rem_card.ui.shared.minigames.bonus_dialog import BonusDialog

            dialog = BonusDialog(role="nurse", data_root_provider=resolve_baza_dir, parent=self)
            dialog.exec()
        except Exception as exc:
            CustomMessageBox.warning(self, "Бонус", f"Не удалось открыть бонус:\n{exc}")

    def on_settings_clicked(self):
        self.layout_manager.set_patient_selection_mode("admin")
        admin_widget = getattr(self.layout_manager, 'admin_widget', None)
        if admin_widget and not self._admin_signals_bound:
            admin_widget.btn_back_to_roles.clicked.connect(self.on_back_clicked)
            self._admin_signals_bound = True
        if admin_widget:
            admin_widget.set_print_context(
                self.remcard_service,
                self.layout_manager.current_admission_id,
                self._current_date,
            )

    def on_add_patient_clicked(self):
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        if not self._acquire_add_patient_lock():
            holder = self._add_patient_lock.describe_holder() if self._add_patient_lock else "другой пользователь"
            CustomMessageBox.warning(
                self,
                "Добавление занято",
                f"Добавление пациента уже открыто на другом рабочем месте.\n\n{holder}",
            )
            self._refresh_add_patient_button_lock_state()
            return
        try:
            self.layout_manager.set_patient_selection_mode(PATIENT_BED_MANAGEMENT_MODE)
        except Exception:
            self._release_add_patient_lock()
            raise

    def _on_selection_mode_changed(self, mode: str):
        self._selection_mode = str(mode or "")
        if self._selection_mode != PATIENT_BED_MANAGEMENT_MODE:
            self._release_add_patient_lock()
        self._refresh_add_patient_button_lock_state()

    def back_to_roles(self):
        if self.parent() and hasattr(self.parent(), 'setCurrentIndex'):
            self.parent().setCurrentIndex(0)

    def on_back_clicked(self):
        current_idx = self.layout_manager.selection_stack.currentIndex()
        journal_idx = -1
        if hasattr(self.layout_manager, "journal_view"):
            journal_idx = self.layout_manager.selection_stack.indexOf(self.layout_manager.journal_view)
        was_journal_mode = (current_idx == journal_idx and journal_idx != -1)

        if current_idx in (0, 2, 3, 4):
            # Явно снимаем lock перед выходом из журнала/режимов выбора.
            self._release_add_patient_lock()
            self.layout_manager.set_patient_selection_mode("beds")
            if was_journal_mode:
                self._force_beds_refresh_after_journal_exit()
        else: 
            self._release_add_patient_lock()
            self.back_to_roles()

    def shutdown(self):
        self._is_closing = True
        if hasattr(self, "_add_patient_lock_watch_timer"):
            self._add_patient_lock_watch_timer.stop()
        self._disconnect_monitor()
        self._release_add_patient_lock()

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)

    def _update_balance_calculations(self):
        """Медсестринская версия расчета баланса для обновления UI секторов."""
        if self._balance_calculator_cls is None:
            from ...services.balance_calculator import BalanceCalculator

            self._balance_calculator_cls = BalanceCalculator

        adm_id = self.layout_manager.current_admission_id
        if not adm_id: return
        runtime = self._balance_runtime_cache or {}
        if not runtime:
            return

        now = datetime.now()
        start = runtime.get("start_dt")
        end = runtime.get("end_dt")
        calc_time = now if start and end and start <= now < end else end
        calc_res = self._balance_calculator_cls.calculate(
            orders=runtime.get("orders") or [],
            current_time=calc_time,
            end_of_card=end,
            transfer_time=runtime.get("transfer_time"),
            active_intervals=runtime.get("active_intervals") or [],
            outcome_time=runtime.get("outcome_time"),
        )
        
        cur, day = calc_res["current"], calc_res["daily"]
        oral_cur = 0
        oral_day = 0
        if hasattr(self.remcard_service, "get_oral_intake_totals"):
            try:
                oral_totals = self.remcard_service.get_oral_intake_totals(
                    adm_id,
                    self._current_date,
                    current_time=calc_time,
                )
                oral_cur = oral_totals.get("current", 0) or 0
                oral_day = oral_totals.get("daily", 0) or 0
            except Exception as exc:
                logger.warning("Failed to load oral intake totals for nurse balance: %s", exc)
        total_in_cur, total_in_day = cur["total"] + oral_cur, day["total"] + oral_day
        total_out_cur = 0
        total_out_day = 0
        
        if hasattr(self, 'balance_controller'): 
            total_out_cur = self.balance_controller.get_total_out_to_now()
            total_out_day = self.balance_controller.get_total_out_daily()
            
        sector_2b_g = getattr(self.layout_manager, 'sector_2b_g', None)
        if sector_2b_g is not None:
            sector_2b_g.update_values(
                infusion=cur["infusion"], preparats=cur["preparats"], blood=cur["blood"], plasma=cur["plasma"],
                infusion_daily=day["infusion"], preparats_daily=day["preparats"], blood_daily=day["blood"], plasma_daily=day["plasma"],
                oral=oral_cur, oral_daily=oral_day
            )
        sector_2b_v = getattr(self.layout_manager, 'sector_2b_v', None)
        if sector_2b_v is not None:
            sector_2b_v.update_balance(total_in_cur, total_out_cur, total_in_daily=total_in_day, total_out_daily=total_out_day)
        sector_3a = getattr(self.layout_manager, 'sector_3a', None)
        if sector_3a is not None:
            sector_3a.update_values(
                total=total_in_cur, infusion=cur["infusion"], preparats=cur["preparats"], blood=cur["blood"], plasma=cur["plasma"],
                total_daily=total_in_day, infusion_daily=day["infusion"], preparats_daily=day["preparats"], blood_daily=day["blood"], plasma_daily=day["plasma"],
                oral=oral_cur, oral_daily=oral_day
            )
        sector_3b = getattr(self.layout_manager, 'sector_3b', None)
        if sector_3b is not None:
            cumulative_out_day = self.balance_controller.get_cumulative_data_daily() if hasattr(self, 'balance_controller') else None
            sector_3b.update_values(total=total_out_day, hour_data=cumulative_out_day)
        sector_4a = getattr(self.layout_manager, 'sector_4a', None)
        if sector_4a is not None:
            sector_4a.update_balance(total_in_cur, total_out_cur, total_in_daily=total_in_day, total_out_daily=total_out_day)
