import os
import socket
from rem_card.ui.shared.async_call import AsyncCallThread
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from PySide6.QtWidgets import QWidget, QVBoxLayout, QDialog
from PySide6.QtCore import Signal, Qt, QTimer
from datetime import datetime
from rem_card.app.logger import logger
from rem_card.app.paths import get_role_lock_path
from rem_card.app.role_session_lock import RoleSessionLock
from rem_card.services.archive_readonly_service import create_archive_readonly_service

ADD_PATIENT_LOCK_POLL_INTERVAL_MS = 1500
ADD_PATIENT_LOCK_KEY = "add_patient_button"
CARD_UI_PREWARM_ENABLED = os.environ.get("REMCARD_CARD_UI_PREWARM", "1") != "0"
CARD_UI_PREWARM_DELAY_MS = max(0, int(os.environ.get("REMCARD_CARD_PREWARM_DELAY_MS", "900")))
CARD_UI_PREWARM_STAGGER_MS = max(0, int(os.environ.get("REMCARD_CARD_PREWARM_STAGGER_MS", "120")))
JOURNAL_PREWARM_DELAY_MS = max(0, int(os.environ.get("REMCARD_JOURNAL_PREWARM_DELAY_MS", "60000")))
JOURNAL_PREWARM_ENABLED = os.environ.get("REMCARD_JOURNAL_PREWARM", "0") == "1"
JOURNAL_WIDGET_PREWARM_ENABLED = os.environ.get("REMCARD_JOURNAL_WIDGET_PREWARM", "0") == "1"

class DoctorRemCardWidget(QWidget):
    archive_requested = Signal()
    back_to_roles_requested = Signal()
    refresh_requested = Signal()

    def __init__(self, remcard_service, admission_id, patient_service=None, parent=None):
        super().__init__(parent)
        self._primary_service = remcard_service
        self.service = remcard_service
        self.admission_id = admission_id
        self.patient_service = patient_service
        self._current_date = datetime.now()
        self._is_loading = False 
        self._last_status = None 
        self._last_sync_time = "1970-01-01 00:00:00.000"
        self._last_change_id = 0
        self._update_scheduled = False
        self._card_widgets_initialized = False
        self._balance_widgets_bound = False
        self._balance_calculator_cls = None
        self._archive_signals_bound = False
        self._admin_signals_bound = False
        self.report_controller = None
        self._card_ui_prewarm_started = False
        self._card_ui_prewarm_done = False
        self._journal_prewarm_started = False
        self._journal_prewarm_done = False
        self._selection_mode = "beds"
        self._archive_read_only_mode = False
        self._archive_source_db_path = None
        self._archive_readonly_db_manager = None
        self._snapshot_worker = None
        self._snapshot_pending = None
        self._create_card_after_snapshot = False
        self._monitor_connected = False
        self._card_snapshot_cache = None
        self._balance_runtime_cache = None
        self._is_closing = False
        self.diet_intake_widget = None
        self._add_patient_lock = self._build_add_patient_lock()
        self._add_patient_lock_held = False
        self._add_patient_locked_by_other = False
        
        logger.debug(f"DoctorRemCardWidget init patient_service={self.patient_service}")
        
        self.init_ui()
        
        # Таймер для обновления баланса (раз в минуту)
        self.balance_timer = QTimer(self)
        self.balance_timer.timeout.connect(self.update_balance_data)

        # Коалесинг пересчета баланса после кликов в назначениях:
        # не блокируем UI тяжелым расчетом на каждый сигнал.
        self._balance_update_delay_ms = 120
        self._balance_update_timer = QTimer(self)
        self._balance_update_timer.setSingleShot(True)
        self._balance_update_timer.timeout.connect(self._flush_scheduled_balance_update)

        # Мониторинг "кнопка-лок" для "Добавить пациента" (между врачом и медсестрой).
        self._add_patient_lock_watch_timer = QTimer(self)
        self._add_patient_lock_watch_timer.timeout.connect(self._refresh_add_patient_button_lock_state)
        self._add_patient_lock_watch_timer.start(ADD_PATIENT_LOCK_POLL_INTERVAL_MS)
        QTimer.singleShot(0, self._refresh_add_patient_button_lock_state)
        if CARD_UI_PREWARM_ENABLED:
            QTimer.singleShot(CARD_UI_PREWARM_DELAY_MS, self._schedule_card_ui_prewarm)
        if JOURNAL_PREWARM_ENABLED:
            QTimer.singleShot(JOURNAL_PREWARM_DELAY_MS, self._schedule_journal_prewarm)

    def _build_add_patient_lock(self) -> RoleSessionLock:
        owner_id = f"{socket.gethostname()}:{os.getpid()}:doctor_add_patient"
        return RoleSessionLock(
            lock_path=get_role_lock_path(ADD_PATIENT_LOCK_KEY),
            role=ADD_PATIENT_LOCK_KEY,
            owner_id=owner_id,
            stale_timeout_sec=60.0,
            heartbeat_sec=8.0,
            logger=logger,
        )

    @staticmethod
    def _is_qobject_alive(obj) -> bool:
        if obj is None:
            return False
        try:
            import shiboken6  # type: ignore

            return bool(shiboken6.isValid(obj))
        except Exception:
            return True

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
            logger.warning("Failed to release add-patient lock (doctor): %s", exc)
        finally:
            self._add_patient_lock_held = False

    def _set_add_patient_button_hint(self, text: str):
        panel = getattr(self, "sector8_panel", None)
        button = getattr(panel, "btn_add_patient", None)
        if self._is_qobject_alive(panel) and self._is_qobject_alive(button):
            # Tooltip intentionally disabled for this button.
            button.setToolTip("")

    def _force_beds_refresh_after_journal_exit(self):
        """Локально обновляет список коек сразу после выхода из режима журнала."""
        data_service = self._get_data_service()
        if data_service:
            try:
                data_service.request_immediate_refresh(force_emit=True)
            except Exception as exc:
                logger.warning("Failed to wake monitor after journal exit (doctor): %s", exc)

        def _refresh():
            try:
                if (
                    hasattr(self, "layout_manager")
                    and hasattr(self.layout_manager, "beds_selection_widget")
                    and self.layout_manager.beds_selection_widget
                ):
                    self.layout_manager.beds_selection_widget.refresh()
            except Exception as exc:
                logger.warning("Failed to refresh beds list after journal exit (doctor): %s", exc)

        QTimer.singleShot(0, _refresh)

    def _resolve_selection_mode(self) -> str:
        """Best-effort определение активного режима по реальному индексу стека."""
        mode = str(self._selection_mode or "")
        layout = getattr(self, "layout_manager", None)
        if not layout or not hasattr(layout, "selection_stack"):
            return mode

        stack = layout.selection_stack
        if not self._is_qobject_alive(stack):
            return mode
        try:
            current_idx = stack.currentIndex()
        except RuntimeError as exc:
            logger.warning("Failed to resolve selection stack current index (doctor): %s", exc)
            return mode

        def safe_index_of(attr_name: str) -> int:
            widget = getattr(layout, attr_name, None)
            if not self._is_qobject_alive(widget):
                return -1
            try:
                return stack.indexOf(widget)
            except RuntimeError as exc:
                logger.warning("Failed to resolve selection stack index %s (doctor): %s", attr_name, exc)
                return -1

        journal_idx = safe_index_of("journal_view")
        beds_idx = safe_index_of("beds_view")
        card_idx = safe_index_of("right_area")
        archive_idx = safe_index_of("archive_view")
        admin_idx = safe_index_of("admin_view")

        if current_idx == journal_idx and journal_idx != -1:
            return "journal"
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
        panel = getattr(self, "sector8_panel", None)
        if not self._is_qobject_alive(panel):
            return
        is_beds_mode = self._selection_mode == "beds"
        enabled = is_beds_mode and not self._add_patient_locked_by_other
        try:
            panel.set_add_patient_enabled(enabled)
        except RuntimeError as exc:
            logger.warning("Failed to update add-patient button state (doctor): %s", exc)
            return

        if self._add_patient_locked_by_other:
            holder = self._add_patient_lock.describe_holder() if self._add_patient_lock else "другой пользователь"
            self._set_add_patient_button_hint(f"Добавление пациента уже открыто.\n{holder}")
        elif is_beds_mode:
            self._set_add_patient_button_hint("Открыть журнал для добавления пациента")
        else:
            self._set_add_patient_button_hint("Кнопка доступна только в режиме списка коек")

    def _refresh_add_patient_button_lock_state(self):
        try:
            if self._is_closing or not self._is_qobject_alive(self):
                return
            resolved_mode = self._resolve_selection_mode()
            if resolved_mode:
                self._selection_mode = resolved_mode

            # Fail-safe: если уже вышли из журнала, lock должен быть снят
            # даже если сигнал смены режима по какой-то причине не пришел.
            if self._selection_mode != "journal" and self._add_patient_lock_held:
                self._release_add_patient_lock()

            locked_by_other = False
            try:
                if self._add_patient_lock and not self._add_patient_lock_held:
                    locked_by_other = self._add_patient_lock.is_held_by_other()
            except Exception as exc:
                logger.warning("Failed to check add-patient lock state (doctor): %s", exc)
            self._add_patient_locked_by_other = bool(locked_by_other)
            self._apply_add_patient_button_state()
        except RuntimeError as exc:
            logger.warning("Failed to refresh add-patient button lock state (doctor): %s", exc)
        except Exception as exc:
            logger.warning("Unexpected add-patient button lock refresh failure (doctor): %s", exc)

    def _get_data_service(self):
        return getattr(self.service, "data_service", None)

    def _get_read_coordinator(self):
        return getattr(self.service, "read_coordinator", None)

    def _current_snapshot_context_key(
        self,
        *,
        admission_id=None,
        shift_date=None,
        load_scope: str = "full",
    ):
        target_admission_id = int(admission_id if admission_id is not None else (self.admission_id or 0))
        target_shift_date = shift_date if shift_date is not None else self._current_date
        source_db = (
            os.path.abspath(str(self._archive_source_db_path))
            if self._archive_read_only_mode and self._archive_source_db_path
            else "live"
        )
        coordinator = self._get_read_coordinator()
        if coordinator is not None and target_shift_date is not None:
            try:
                context = coordinator.make_patient_snapshot_context(
                    source_db=source_db,
                    admission_id=target_admission_id,
                    shift_date=target_shift_date,
                    role="doctor",
                    mode="archive" if self._archive_read_only_mode else "live",
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
            "doctor",
            "archive" if self._archive_read_only_mode else "live",
            source_db,
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
        show_empty_message: bool = False,
        force_emit: bool = False,
        load_scope: str = "full",
    ):
        if not self.admission_id:
            return

        request = {
            "admission_id": int(self.admission_id),
            "shift_date": self._current_date,
            "ensure_initial_status": bool(ensure_initial_status),
            "show_empty_message": bool(show_empty_message),
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

    def _build_card_snapshot_job(self, request: dict):
        load_scope = str(request.get("load_scope") or "full")
        if load_scope == "patient_open_vitals":
            coordinator = self._get_read_coordinator()
            if coordinator is not None:
                snapshot = coordinator.load_patient_vitals_snapshot(
                    request["admission_id"],
                    request["shift_date"],
                    role="doctor",
                    mode="archive" if self._archive_read_only_mode else "live",
                    source_db=self._archive_source_db_path if self._archive_read_only_mode else "live",
                    ensure_initial_status=request["ensure_initial_status"],
                    force_refresh=True,
                )
            else:
                logger.warning(
                    "DoctorRemCardWidget: ReadCoordinator unavailable, using legacy build_card_snapshot for patient open"
                )
                snapshot = self.service.build_card_snapshot(
                    request["admission_id"],
                    request["shift_date"],
                    include_change_cursor=True,
                    include_balance=True,
                    balance_only_committed=False,
                    ensure_initial_status=request["ensure_initial_status"],
                )
        else:
            snapshot = self.service.build_card_snapshot(
                request["admission_id"],
                request["shift_date"],
                include_change_cursor=True,
                include_balance=True,
                balance_only_committed=False,
                ensure_initial_status=request["ensure_initial_status"],
            )
        request["snapshot"] = snapshot
        return request

    def _apply_card_snapshot(self, request: dict):
        if (
            int(request["admission_id"]) != int(self.admission_id or 0)
            or request["shift_date"] != self._current_date
        ):
            return
        if request.get("context_key") != self._current_snapshot_context_key(
            load_scope=request.get("load_scope", "full")
        ):
            logger.info(
                "DoctorRemCardWidget discarded stale snapshot admission_id=%s load_scope=%s request_context=%s current_context=%s",
                request.get("admission_id"),
                request.get("load_scope"),
                request.get("context_key"),
                self._current_snapshot_context_key(load_scope=request.get("load_scope", "full")),
            )
            return

        snapshot = dict(request.get("snapshot") or {})
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
            self.vitals_input.admission_id = self.admission_id
            self.vitals_input.shift_date = self._current_date
            self.vitals_input.apply_context_snapshot(
                patient=snapshot.get("patient"),
                settings=snapshot.get("settings") or {},
                effective_bounds=effective_bounds,
                has_vitals=bool(snapshot.get("has_vitals")),
            )

        if hasattr(self, 'layout_manager') and hasattr(self.layout_manager, 'sector_2a'):
            self.layout_manager.sector_2a.update_period(snapshot.get("start_dt"))

        if hasattr(self, "balance_controller") and effective_bounds and snapshot.get("fluids") is not None:
            self.balance_controller.apply_loaded_data(
                snapshot.get("fluids") or [],
                effective_bounds,
            )

        self.update_patient_info()
        self.update_latest_indicators()
        self.update_balance_data()

        if hasattr(self.layout_manager, "set_current_status_dto"):
            self.layout_manager.set_current_status_dto(snapshot.get("status"))
        if snapshot.get("status"):
            self._last_status = snapshot["status"].status
            self._update_ui_accessibility(snapshot["status"])

        self._last_change_id = max(
            int(self._last_change_id or 0),
            int(snapshot.get("change_id") or 0),
        )

        if request.get("show_empty_message") and not snapshot.get("vitals"):
            CustomMessageBox.information(
                self,
                "Пусто",
                f"Нет данных за {self._current_date.strftime('%d.%m.%Y')}",
            )

    def _on_card_snapshot_failed(self, exc: Exception):
        logger.error("DoctorRemCardWidget snapshot load failed: %s", exc, exc_info=True)

    def _on_card_snapshot_finished(self, worker):
        if self._snapshot_worker is worker:
            self._snapshot_worker = None
        if self._is_closing:
            self._snapshot_pending = None
            self._create_card_after_snapshot = False
            return
        if self._create_card_after_snapshot:
            self._create_card_after_snapshot = False
            self._snapshot_pending = None
            QTimer.singleShot(0, self.on_create_card_clicked)
            return
        if self._snapshot_pending:
            pending = self._snapshot_pending
            self._snapshot_pending = None
            QTimer.singleShot(
                0,
                lambda req=pending: self._request_card_snapshot(
                    ensure_initial_status=req["ensure_initial_status"],
                    show_empty_message=req["show_empty_message"],
                    load_scope=req.get("load_scope", "full"),
                ),
            )

    def _reset_balance_view_state(self):
        if hasattr(self, "balance_controller") and self.balance_controller:
            self.balance_controller.hourly_cache = self.balance_controller._build_empty_hourly_cache()
            self.balance_controller._effective_bounds_cache = None
            if getattr(self.balance_controller, "quick_input", None):
                self.balance_controller.quick_input.update_quick_values({})

        sector_2b_g = getattr(self.layout_manager, "sector_2b_g", None)
        if sector_2b_g is not None:
            sector_2b_g.update_values()
        sector_2b_v = getattr(self.layout_manager, "sector_2b_v", None)
        if sector_2b_v is not None:
            sector_2b_v.update_balance(0, 0, 0, 0)
            sector_2b_v.update_quick_values({})
        sector_3a = getattr(self.layout_manager, "sector_3a", None)
        if sector_3a is not None:
            sector_3a.update_values(0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        sector_3b = getattr(self.layout_manager, "sector_3b", None)
        if sector_3b is not None:
            sector_3b.update_values(0, {})
        sector_4a = getattr(self.layout_manager, "sector_4a", None)
        if sector_4a is not None:
            sector_4a.update_balance(0, 0, 0, 0)

    def _payload_is_relevant(self, payload: dict) -> bool:
        if self._archive_read_only_mode or not self.admission_id:
            return False
        if payload.get("forced"):
            return True

        relevant_entities = {"patients", "admissions", "beds", "operations", "diet_templates"}
        orders_entities = {"orders", "administrations"}
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
        if changed_entities.intersection(orders_entities | {"diet_templates"}) and not payload.get("changes"):
            return True
        return False

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
        orders_entities = {"orders", "administrations"}
        if self._selection_mode == "archive" and (payload.get("forced") or changed_entities.intersection({"patients", "admissions"})):
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
        diet_entities = {"diet_templates", "diet_plan", "oral_intake_events"}
        has_diet_changes = bool(changed_entities.intersection(diet_entities))
        if payload.get("forced") and getattr(self, "diet_intake_widget", None):
            self.diet_intake_widget.handle_data_changes(payload)
        if has_diet_changes and getattr(self, "diet_intake_widget", None):
            self.diet_intake_widget.handle_data_changes(payload)
            if "oral_intake_events" in changed_entities:
                self.update_balance_data()
            if not payload.get("forced") and set(changed_entities).issubset(diet_entities):
                return
        has_orders_changes = bool(changed_entities.intersection(orders_entities))
        non_orders_changes = set(changed_entities) - orders_entities
        if (payload.get("forced") or has_orders_changes) and hasattr(self.layout_manager, 'orders_widget'):
            try:
                self.layout_manager.orders_widget.handle_data_changes(
                    payload,
                    tab_active=self._is_orders_tab_active(),
                )
            except Exception:
                logger.exception("Orders delta refresh failed")
        if (payload.get("forced") or has_orders_changes) and hasattr(self.layout_manager, "nurse_orders_manager"):
            try:
                mgr = self.layout_manager.nurse_orders_manager
                if mgr and hasattr(mgr, "handle_data_changes"):
                    mgr.handle_data_changes(payload)
            except Exception:
                logger.exception("Current nurse orders refresh failed")
        self._request_card_snapshot(show_empty_message=False)

    def start_polling(self):
        """Подписывает карту на сервисный monitor и оставляет только чистый UI-таймер баланса."""
        if self._archive_read_only_mode:
            self.stop_polling()
            return
        self._ensure_monitor_subscription()
        if not self.balance_timer.isActive():
            self.balance_timer.start(60000)
        data_service = self._get_data_service()
        if data_service:
            data_service.request_immediate_refresh(force_emit=True)

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
            from ..shared.report_controller import RemCardReportController

            self.report_controller = RemCardReportController(self.service, self)
        return self.report_controller

    def _schedule_balance_update(self, *_args):
        if not self.admission_id:
            return
        self._balance_update_timer.start(self._balance_update_delay_ms)

    def _flush_scheduled_balance_update(self):
        if not self.admission_id:
            return
        self.update_balance_data()

    def _set_service_context(self, service):
        self.service = service
        if hasattr(self, "layout_manager") and self.layout_manager:
            self.layout_manager.remcard_service = service
            self.layout_manager.patient_status_service = getattr(service, "status_service", None)
            if hasattr(self.layout_manager, "beds_selection_widget") and self.layout_manager.beds_selection_widget:
                self.layout_manager.beds_selection_widget.remcard_service = service
            if hasattr(self.layout_manager, "orders_widget") and self.layout_manager.orders_widget:
                self.layout_manager.orders_widget.service = service

        if hasattr(self, "vitals_input") and self.vitals_input:
            self.vitals_input.service = service
        if hasattr(self, "chart") and self.chart:
            self.chart.service = service
            self.chart.status_service = getattr(service, "status_service", None)
        if hasattr(self, "balance_controller") and self.balance_controller:
            self.balance_controller.service = service._fluids
        if getattr(self, "diet_intake_widget", None):
            self.diet_intake_widget.set_service(service)

        self.report_controller = None

    def _ensure_diet_widget(self):
        if getattr(self, "diet_intake_widget", None) is not None:
            return self.diet_intake_widget
        if not hasattr(self, "layout_manager") or not hasattr(self.layout_manager, "sector_5"):
            return None
        from rem_card.ui.shared.components.diet_intake_widget import DietIntakeWidget

        self.diet_intake_widget = DietIntakeWidget(self.service, role="doctor")
        self.diet_intake_widget.data_changed.connect(self.update_balance_data)
        self.layout_manager.sector_5.set_content(self.diet_intake_widget)
        return self.diet_intake_widget

    def _close_archive_readonly_manager(self):
        if self._archive_readonly_db_manager:
            try:
                self._archive_readonly_db_manager.close()
            except Exception as exc:
                logger.warning("Failed to close archive read-only DB manager: %s", exc)
        self._archive_readonly_db_manager = None

    def _enter_archive_read_only_mode(self, source_db_path: str):
        src = str(source_db_path or "").strip()
        if not src:
            raise ValueError("Archive DB path is empty")
        abs_src = os.path.abspath(src)
        current_src = os.path.abspath(str(self._archive_source_db_path or ""))

        if self._archive_read_only_mode and abs_src == current_src:
            return

        self.stop_polling()
        self._close_archive_readonly_manager()

        ro_service, ro_db_manager = create_archive_readonly_service(abs_src)
        self._archive_readonly_db_manager = ro_db_manager
        self._archive_read_only_mode = True
        self._archive_source_db_path = abs_src
        self._set_service_context(ro_service)
        self._apply_archive_read_only_state()

    def _exit_archive_read_only_mode(self):
        if not self._archive_read_only_mode:
            return

        self.stop_polling()
        self._close_archive_readonly_manager()
        self._archive_read_only_mode = False
        self._archive_source_db_path = None
        self._set_service_context(self._primary_service)
        self._apply_archive_read_only_state()

    def _show_read_only_hint(self):
        CustomMessageBox.information(
            self,
            "Только чтение",
            "Архивная карта открыта в режиме только чтения.",
        )

    def _apply_archive_read_only_state(self):
        read_only = bool(self._archive_read_only_mode)

        if hasattr(self, "vitals_input") and self.vitals_input:
            if hasattr(self.vitals_input, "set_forced_read_only"):
                self.vitals_input.set_forced_read_only(read_only)
            else:
                self.vitals_input.setEnabled(not read_only)

        if hasattr(self, "layout_manager") and hasattr(self.layout_manager, "orders_widget"):
            ow = self.layout_manager.orders_widget
            if ow:
                if hasattr(ow, "set_forced_read_only"):
                    ow.set_forced_read_only(read_only)
                else:
                    ow.setEnabled(not read_only)

        if hasattr(self, "layout_manager"):
            events_sector = getattr(self.layout_manager, "sector_events", None)
            if events_sector:
                events_sector.setEnabled(not read_only)
        if getattr(self, "diet_intake_widget", None):
            self.diet_intake_widget.set_read_only(read_only)

        if hasattr(self, "controls") and self.controls:
            if read_only:
                self.controls.btn_save.setEnabled(False)
                self.controls.btn_clean_sheet.setEnabled(False)
                self.controls.btn_clear.setEnabled(False)
                self.controls.btn_yesterday.setEnabled(False)
                self.controls.btn_rollback.setEnabled(False)
                self.controls.btn_templates.setEnabled(False)
                self.controls.btn_pokaz.setEnabled(False)
            else:
                ow = self.layout_manager.orders_widget if hasattr(self.layout_manager, "orders_widget") else None
                has_drafts = ow.has_drafts() if ow else False
                has_admins = ow.has_administrations() if ow else False
                has_orders = ow.has_orders() if ow else False
                self.controls.set_save_active(has_drafts)
                self.controls.set_rollback_active(has_drafts)
                self.controls.set_clean_active(has_admins)
                self.controls.set_clear_active(has_orders)
                self.controls.set_templates_active(True)
                self.controls.btn_pokaz.setEnabled(True)
                self._update_yesterday_button_state()

        if hasattr(self, "layout_manager") and hasattr(self.layout_manager, "sector_4v"):
            s4v = self.layout_manager.sector_4v
            snapshot = self._card_snapshot_cache or {}
            card_exists = bool(snapshot.get("card_exists"))
            yest_exists = bool(snapshot.get("yest_exists"))

            # Сохраняем бизнес-логику 4в (наличие карт), добавляя только ограничение read-only.
            s4v.set_buttons_state(card_exists, yest_exists)
            if read_only:
                s4v.btn_new_card.setEnabled(False)
            s4v.btn_card_list.setEnabled(True)
            s4v.btn_daily_print.setEnabled(True)
            s4v.btn_all_print.setEnabled(True)

    def _resolve_archive_open_date(self, admission_id: int, fallback_patient=None) -> datetime:
        try:
            card_dates = self.service.get_all_card_dates(admission_id)
            if card_dates:
                return max(card_dates)
        except Exception as exc:
            logger.warning("Failed to resolve latest card date in archive DB: %s", exc)

        if fallback_patient and getattr(fallback_patient, "admission_datetime", None):
            return fallback_patient.admission_datetime
        return datetime.now()

    def load_patient_card(self, admission_id, date, *, request_snapshot: bool = True, ensure_initial_status=None):
        """Обновляет данные карты для нового пациента/даты."""
        self._schedule_card_ui_prewarm()
        self._ensure_card_widgets_initialized()
        from rem_card.app.logger import logger
        logger.info(f"[DOCTOR_VIEW] Loading patient card. AdmID: {admission_id}, Date: {date}")

        self._balance_update_timer.stop()

        if hasattr(self.layout_manager, 'orders_widget') and not self._archive_read_only_mode:
            self.layout_manager.orders_widget.clear_drafts()
        
        self.admission_id = admission_id
        self.current_date = date
        self._card_snapshot_cache = None
        self._balance_runtime_cache = None
        try:
            card_start_dt, card_end_dt = self.service.get_day_period(date)
        except Exception:
            card_start_dt, card_end_dt = date, None

        # Интеграция событий статуса
        self.layout_manager.current_admission_id = admission_id
        if hasattr(self, 'chart'):
            self.chart.admission_id = admission_id
            if hasattr(self.chart, "clear_for_context"):
                self.chart.clear_for_context(admission_id=admission_id, start_time=card_start_dt)

        if hasattr(self.layout_manager, "set_events_context"):
            self.layout_manager.set_events_context(
                admission_id=admission_id,
                status_service=self.service.status_service,
                shift_date=date,
                shift_start=card_start_dt,
                shift_end=card_end_dt,
            )
        
        if hasattr(self, 'vitals_input'):
            self.vitals_input.admission_id = admission_id
            self.vitals_input.shift_date = date
            self.vitals_input.mark_dirty()
            
        if hasattr(self.layout_manager, 'orders_widget'):
            ow = self.layout_manager.orders_widget
            if hasattr(ow, "set_context"):
                ow.set_context(
                    service=self.service,
                    admission_id=admission_id,
                    shift_date=date,
                )
            else:
                ow.service = self.service
                ow.admission_id = admission_id
                ow.shift_date = date
            if not self._archive_read_only_mode:
                ow.clear_drafts()

        self._last_change_id = 0
        self._apply_archive_read_only_state()
        self._reset_balance_view_state()
        if request_snapshot:
            should_ensure_initial_status = (
                not self._archive_read_only_mode
                if ensure_initial_status is None
                else bool(ensure_initial_status)
            )
            self._request_card_snapshot(
                ensure_initial_status=should_ensure_initial_status,
                show_empty_message=False,
                load_scope="patient_open_vitals",
            )
        
        if hasattr(self, 'layout_manager'):
            self.layout_manager.set_active_tab("Витальные функции")
            if hasattr(self.layout_manager, 'sector_2b'):
                self.layout_manager.sector_2b.on_tab_clicked("Витальные функции")

        if hasattr(self, 'balance_controller'):
            self.balance_controller.admission_id = admission_id
            self.balance_controller.shift_date = date

        diet_widget = self._ensure_diet_widget()
        if diet_widget:
            diet_widget.set_context(admission_id, date)

        # Обновляем контекст 1а/5 явно: это нужно и при ПЕРВОМ открытии карты.
        # Важно: manager может еще не существовать, если мы пришли из списка коек.
        nurse_orders_mgr = None
        if hasattr(self.layout_manager, "ensure_nurse_orders_manager"):
            nurse_orders_mgr = self.layout_manager.ensure_nurse_orders_manager()
        elif hasattr(self.layout_manager, "nurse_orders_manager"):
            nurse_orders_mgr = self.layout_manager.nurse_orders_manager
        if nurse_orders_mgr:
            QTimer.singleShot(0, lambda mgr=nurse_orders_mgr, aid=admission_id, d=date: mgr.set_context(aid, d))
        
        # Запуск фонового обновления
        QTimer.singleShot(0, self.start_polling)

    def _prime_patient_header_from_w1(self, patient, target_date):
        """Заполняет 4б/4в данными уже отрисованной W1-строки до показа карты."""
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
                        target_start, _ = self.service.get_day_period(target_date)
                        runtime_start, _ = self.service.get_day_period(runtime_now)
                        same_shift = target_start == runtime_start
                    except Exception:
                        same_shift = False

                if same_shift and ("card_exists" in runtime or "yest_exists" in runtime):
                    layout.sector_4v.set_buttons_state(
                        bool(runtime.get("card_exists")),
                        bool(runtime.get("yest_exists")),
                    )

            logger.info(
                "[DOCTOR_VIEW] primed patient header from W1 admission_id=%s has_runtime=%s",
                getattr(patient, "id", None),
                int(bool(runtime)),
            )
        except Exception as exc:
            logger.warning("Failed to prime patient header from W1: %s", exc, exc_info=True)

    def perform_coalesced_update(self):
        self._update_scheduled = False
        self._request_card_snapshot(show_empty_message=False)

    def _update_ui_accessibility(self, status):
        """Блокирует или разблокирует ввод в зависимости от статуса."""
        if self._archive_read_only_mode:
            self._apply_archive_read_only_state()
            return

        # ИСХОД (Переведен или Умер)
        status_value = getattr(status, "status", status)
        is_outcome = bool(status_value and getattr(status_value, "is_outcome", lambda: False)())
        
        # Сектор 1б (Ввод витальных функций) теперь доступен ВСЕГДА
        if hasattr(self, 'vitals_input'):
            self.vitals_input.setEnabled(True)
            
        # Вкладка Назначения теперь доступна ВСЕГДА (врач может править даже после исхода)
        if hasattr(self.layout_manager, 'orders_widget'):
            self.layout_manager.orders_widget.setEnabled(True)

        # Панель управления (save/clean/clear) доступна всегда
        if hasattr(self, 'controls'):
            self.controls.setEnabled(True)

    @property
    def current_date(self):
        return self._current_date

    @current_date.setter
    def current_date(self, value):
        if self._current_date == value:
            return
        self._current_date = value
        if hasattr(self, 'date_info_lbl'):
            self.date_info_lbl.setText(f"Дата карты: {value.strftime('%d.%m.%Y')}")
        if hasattr(self, 'vitals_input'):
            self.vitals_input.admission_id = self.admission_id
            self.vitals_input.shift_date = self._current_date
            self.vitals_input.mark_dirty()
        if hasattr(self.layout_manager, "set_events_context"):
            s_start, s_end = self.service.get_day_period(value)
            self.layout_manager.set_events_context(
                shift_date=value,
                shift_start=s_start,
                shift_end=s_end,
            )
        diet_widget = self._ensure_diet_widget()
        if diet_widget and self.admission_id:
            diet_widget.set_context(self.admission_id, self._current_date)
        # Критично: сектор 5 (история) должен строго следовать дате открытой карты,
        # иначе при переходе в архив может остаться контекст "сегодня".
        if (
            hasattr(self, 'layout_manager')
            and hasattr(self.layout_manager, 'nurse_orders_manager')
            and self.admission_id
        ):
            mgr = self.layout_manager.nurse_orders_manager
            if mgr:
                mgr.set_context(self.admission_id, self._current_date)

    def init_ui(self):
        from ..shared.remcard_layout import RemCardLayoutManager
        from .components.control_panel import ControlPanel
        from .components.sector8_panel import Sector8Panel

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(5, 5, 5, 5)
        
        # Передаем remcard_service В конструктор, чтобы он был доступен СРАЗУ при создании секторов
        p_service = self.patient_service
        self.layout_manager = RemCardLayoutManager(role="Врач", patient_service=p_service, remcard_service=self.service)
        self.layout_manager.patient_status_service = self.service.status_service
        self.layout_manager.current_admission_id = self.admission_id
        
        main_layout.addWidget(self.layout_manager)

        if hasattr(self.layout_manager, 'orders_widget'):
            self.layout_manager.orders_widget.service = self.service

        self._ensure_diet_widget()

        self.controls = ControlPanel(orientation=Qt.Vertical)
        self.controls.btn_yesterday.setText(" Вчерашнее")
        self.controls.btn_rollback.setText(" Отмена")
        self.sector8_panel = Sector8Panel()
        self.btn_back = self.sector8_panel.btn_back
        self.btn_settings = self.sector8_panel.btn_settings
        self.btn_exit = self.sector8_panel.btn_exit
        
        self.btn_exit.clicked.connect(self.on_exit_clicked)
        self.btn_back.clicked.connect(self.on_back_clicked)
        self.btn_settings.clicked.connect(self.on_settings_clicked)
        self.sector8_panel.archive_clicked.connect(self.on_global_archive_clicked)
        self.sector8_panel.refresh_clicked.connect(self.on_refresh_beds_clicked)
        self.sector8_panel.calc_clicked.connect(self.on_calculator_clicked)
        self.sector8_panel.add_patient_clicked.connect(self.on_add_patient_clicked)
        self.sector8_panel.bonus_clicked.connect(self.on_bonus_clicked)

        if hasattr(self.layout_manager, 'beds_selection_widget'):
            self.layout_manager.beds_selection_widget.patient_selected.connect(self.on_patient_selected_from_list)

        if hasattr(self.layout_manager, 'sector_8'):
            self.layout_manager.sector_8.set_content(self.sector8_panel)
        if hasattr(self.layout_manager, "selection_mode_changed"):
            self.layout_manager.selection_mode_changed.connect(self._on_selection_mode_changed)
            self._on_selection_mode_changed(getattr(self.layout_manager, "current_mode", "beds"))

        if hasattr(self.layout_manager, 'orders_widget'):
            ow = self.layout_manager.orders_widget
            ow.draftStatusChanged.connect(self.controls.set_save_active)
            ow.draftStatusChanged.connect(self.controls.set_rollback_active)
            ow.administrationStatusChanged.connect(self.controls.set_clean_active)
            ow.ordersPresenceChanged.connect(self.controls.set_clear_active)
            ow.draftStatusChanged.connect(self._schedule_balance_update)
            ow.administrationStatusChanged.connect(self._schedule_balance_update)
            ow.ordersPresenceChanged.connect(self._schedule_balance_update)
            self.controls.btn_save.clicked.connect(ow.finalize_card)
            self.controls.btn_clean_sheet.clicked.connect(self.on_clean_sheet_clicked)
            self.controls.btn_clear.clicked.connect(self.on_clear_orders_clicked)
            self.controls.btn_yesterday.clicked.connect(ow.load_yesterday_orders)
            self.controls.btn_rollback.clicked.connect(self.on_rollback_clicked)
            self.controls.btn_templates.clicked.connect(ow.open_template_dialog)
            has_drafts = ow.has_drafts()
            self.controls.set_save_active(has_drafts)
            self.controls.set_rollback_active(has_drafts)
            self.controls.set_clean_active(ow.has_administrations())
            self.controls.set_clear_active(ow.has_orders())

        if (
            hasattr(self.layout_manager, 'sector_7na_b')
            and hasattr(self.layout_manager.sector_7na_b, 'data_layout')
        ):
            self.layout_manager.sector_7na_b.data_layout.addWidget(self.controls)

        if hasattr(self.layout_manager, 'sector_2b'):
            self.layout_manager.sector_2b.tab_changed.connect(self.on_tab_changed)

        if hasattr(self.layout_manager, "register_events_status_handler"):
            self.layout_manager.register_events_status_handler(self.force_reload_all)
        elif hasattr(self.layout_manager, 'sector_events') and self.layout_manager.sector_events:
            self.layout_manager.sector_events.status_changed.connect(self.force_reload_all)

        if hasattr(self.layout_manager, 'sector_3b'):
            self.layout_manager.sector_3b.out_values_changed.connect(self.on_out_values_changed)

        if hasattr(self.layout_manager, 'sector_4v'):
            s4v = self.layout_manager.sector_4v
            s4v.archive_requested.connect(self.show_archive)
            s4v.show_card_requested.connect(self.on_show_card_clicked)
            s4v.create_card_requested.connect(self.on_create_card_clicked)
            s4v.yest_card_requested.connect(self.on_yest_card_clicked)
            s4v.full_report_requested.connect(self.on_full_report_clicked)
            s4v.daily_report_requested.connect(self.on_daily_report_clicked)

        # Убираем тяжелые вызовы из __init__
        self._wire_dynamic_views()

    def _wire_dynamic_views(self):
        archive_widget = getattr(self.layout_manager, "archive_widget", None)
        if archive_widget and not self._archive_signals_bound:
            archive_widget.back_requested.connect(lambda: self.on_back_clicked())
            archive_widget.patient_selected.connect(self.on_patient_selected_from_archive)
            self._archive_signals_bound = True

        admin_widget = getattr(self.layout_manager, "admin_widget", None)
        if admin_widget and not self._admin_signals_bound:
            admin_widget.btn_back_to_roles.clicked.connect(lambda: self.on_back_clicked())
            self._admin_signals_bound = True

    def _on_selection_mode_changed(self, mode: str):
        self._selection_mode = str(mode or "")
        if self._selection_mode != "journal":
            self._release_add_patient_lock()
        self._refresh_add_patient_button_lock_state()

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
            logger.warning("Doctor card UI prewarm stage1 failed: %s", exc)
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
            logger.warning("Doctor card UI prewarm stage2 failed: %s", exc)
            self._card_ui_prewarm_started = False

    def _run_card_ui_prewarm_stage_3(self):
        if self._card_ui_prewarm_done:
            return
        try:
            if hasattr(self.layout_manager, 'ensure_nurse_orders_manager'):
                self.layout_manager.ensure_nurse_orders_manager()
            self._card_ui_prewarm_done = True
            logger.debug("Doctor card UI prewarm completed")
        except Exception as exc:
            logger.warning("Doctor card UI prewarm stage3 failed: %s", exc)
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
            from ..shared.journal_integration import warmup_journal_services_async

            warmup_journal_services_async()
            if not JOURNAL_WIDGET_PREWARM_ENABLED:
                self._journal_prewarm_done = True
                return

            if hasattr(self, "layout_manager") and hasattr(self.layout_manager, "prewarm_journal_widget"):
                self.layout_manager.prewarm_journal_widget()
                self._journal_prewarm_done = True
                logger.debug("Doctor journal widget prewarm completed")
        except Exception as exc:
            logger.warning("Doctor journal prewarm failed: %s", exc)
        finally:
            if not self._journal_prewarm_done:
                self._journal_prewarm_started = False

    def _ensure_card_widgets_initialized(self):
        if self._card_widgets_initialized:
            return

        from ..shared.chart_widget import ChartWidget
        from rem_card.ui.shared.vitals_widget import VitalsWidget
        from .components.balance_controller import BalanceController

        self.chart = ChartWidget()
        self.chart.service = self.service
        self.chart.status_service = self.service.status_service
        self.chart.admission_id = self.admission_id
        self.layout_manager.sector_2v.set_content(self.chart)

        self.vitals_input = VitalsWidget(self.service, self.admission_id, self._current_date)
        self.vitals_input.save_btn.clicked.connect(self.refresh_data)
        self.vitals_input.data_changed.connect(self.refresh_data)
        self.layout_manager.sector_1b.set_content(self.vitals_input)

        self.balance_controller = BalanceController(self.service._fluids, self.admission_id, self._current_date)
        self._bind_balance_widgets_if_ready()

        self._card_widgets_initialized = True

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
        self.balance_controller.data_updated.connect(self.update_balance_data)
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
        if not self._bind_balance_widgets_if_ready():
            return
        if hasattr(self, "balance_controller") and self.balance_controller:
            self.balance_controller.admission_id = self.admission_id
            self.balance_controller.shift_date = self._current_date
        if not self._apply_balance_snapshot_if_available():
            self._request_card_snapshot(load_scope="full")

    def on_show_card_clicked(self):
        now = datetime.now()
        start, end = self.service.get_day_period(now)
        if not (start <= self._current_date < end):
            if self.service.has_card(self.admission_id, now):
                self.safe_load_archived_card(now)
            else:
                CustomMessageBox.information(self, "Пусто", "Карта за текущие сутки еще не создана.")
        else:
            self.refresh_data(show_empty_message=True)

    def on_create_card_clicked(self):
        if self._archive_read_only_mode:
            self._show_read_only_hint()
            return
        if self._snapshot_worker and self._snapshot_worker.isRunning():
            if not self._create_card_after_snapshot:
                logger.info(
                    "DoctorRemCardWidget defers create-card write until snapshot load finishes admission_id=%s",
                    self.admission_id,
                )
            self._create_card_after_snapshot = True
            self._snapshot_pending = None
            return
        if self.admission_id and self.service.status_service:
            now = datetime.now()
            start, _ = self.service.get_day_period(now)
            patient = self.service.get_patient(self.admission_id)
            adm_dt = patient.admission_datetime if patient else None
            self.service.status_service.ensure_initial_status(self.admission_id, start, adm_dt)
            self.layout_manager.refresh_current_status()

        now = datetime.now()
        start, _ = self.service.get_day_period(now)
        patient = self.service.get_patient(self.admission_id)
        vital_time = start
        if patient and patient.admission_datetime and start < patient.admission_datetime:
            vital_time = patient.admission_datetime
            
        from rem_card.data.dto.remcard_dto import VitalDTO
        dto = VitalDTO(id=None, admission_id=self.admission_id, timestamp=vital_time,
                       sys=None, dia=None, pulse=None, temp=None, spo2=None, rr=None, cvp=None)
        
        try:
            self.service.add_vital(dto, shift_date=now, force=True)
            if hasattr(self, 'vitals_input'):
                self.vitals_input.update_undo_button_state()
                self.vitals_input.data_changed.emit()
        except Exception as e:
            from ...app.logger import logger
            logger.error(f"Error creating empty vital for card: {e}", exc_info=True)
            
        self.update_patient_info()
        CustomMessageBox.information(self, "Создание карты", "Карта успешно создана. Вы можете приступить к её заполнению.")

    def on_yest_card_clicked(self):
        from datetime import timedelta
        yest = self._current_date - timedelta(days=1)
        logger.info(
            "[DOCTOR_VIEW] yesterday card requested from card admission_id=%s target_date=%s",
            self.admission_id,
            yest.isoformat() if hasattr(yest, "isoformat") else yest,
        )
        QTimer.singleShot(0, lambda target_date=yest: self.safe_load_archived_card(target_date))

    def show_archive(self, patient=None):
        if self._is_loading: return
        from ...app.logger import logger
        from .card_list_widget import PatientArchiveDialog
        try:
            if not patient:
                patient = self.service.get_patient(self.admission_id)
            
            if not patient:
                CustomMessageBox.warning(self, "Ошибка", "Пациент не найден.")
                return
                
            dialog = PatientArchiveDialog(self.service, patient, self)
            dialog.setAttribute(Qt.WA_DeleteOnClose)
            result = dialog.exec()
            
            if result == QDialog.Accepted:
                selected_date = dialog.get_selected_date()
                if selected_date:
                    target_dt = datetime.fromtimestamp(selected_date.timestamp())
                    # Если мы открываем из списка коек (где карта еще не загружена) или дата отличается
                    if patient.id != self.admission_id or target_dt != self._current_date:
                        QTimer.singleShot(100, lambda: self.safe_load_archived_card(target_dt, patient.id))
        except Exception as e:
            logger.error(f"Error showing archive: {e}", exc_info=True)

    def safe_load_archived_card(self, selected_date, admission_id=None):
        if self._is_loading: return
        self._ensure_card_widgets_initialized()
        from ...app.logger import logger
        self._is_loading = True
        
        target_id = admission_id if admission_id is not None else self.admission_id
        
        ow = None
        if hasattr(self.layout_manager, 'orders_widget'):
            ow = self.layout_manager.orders_widget
        try:
            logger.info(
                "[ARCHIVE] loading card admission_id=%s date=%s",
                target_id,
                selected_date.isoformat() if hasattr(selected_date, "isoformat") else selected_date,
            )
            self.blockSignals(True)
            if ow: 
                ow.blockSignals(True)
                ow.stop_timer()
            
            if target_id and self.service.status_service and not self._archive_read_only_mode:
                current_start, current_end = self.service.get_day_period(datetime.now())
                if current_start <= selected_date < current_end:
                    s_start, _ = self.service.get_day_period(selected_date)
                    patient = self.service.get_patient(target_id)
                    adm_dt = patient.admission_datetime if patient else None
                    self.service.status_service.ensure_initial_status(target_id, s_start, adm_dt)
                else:
                    logger.info(
                        "[ARCHIVE] skip initial status write for historical card admission_id=%s date=%s",
                        target_id,
                        selected_date.isoformat() if hasattr(selected_date, "isoformat") else selected_date,
                    )

            if admission_id is not None:
                self.admission_id = admission_id
                self.layout_manager.current_admission_id = admission_id
                self.layout_manager.set_patient_selection_mode("card")
                self.layout_manager.bottom_row.show()

            self.current_date = selected_date
            if hasattr(self.layout_manager, 'nurse_orders_manager') and self.layout_manager.nurse_orders_manager:
                self.layout_manager.nurse_orders_manager.set_context(target_id, self._current_date)
            self.force_reload_all()
            self._update_yesterday_button_state()
            self._apply_archive_read_only_state()
        except Exception as e:
            logger.error(f"[ARCHIVE] !!! CRITICAL ERROR: {e}", exc_info=True)
            CustomMessageBox.critical(self, "Ошибка", f"Произошла ошибка при загрузке карты: {e}")
        finally:
            if ow: 
                ow.blockSignals(False)
                ow.start_timer()
            self.blockSignals(False)
            self._is_loading = False
            self.update()
            logger.info(
                "[ARCHIVE] card load finished admission_id=%s date=%s",
                target_id,
                selected_date.isoformat() if hasattr(selected_date, "isoformat") else selected_date,
            )

    def force_reload_all(self):
        self._ensure_card_widgets_initialized()
        from ...app.logger import logger
        logger.debug("[RELOAD] --- Beginning full reload sequence ---")
        try:
            if hasattr(self, 'balance_controller'):
                self.balance_controller.shift_date = self._current_date
                
            self._request_card_snapshot(
                ensure_initial_status=not self._archive_read_only_mode,
                show_empty_message=False,
                force_emit=True,
            )
            
            if hasattr(self.layout_manager, 'orders_widget'):
                ow = self.layout_manager.orders_widget
                ow.blockSignals(True)
                try:
                    if hasattr(ow, "set_context"):
                        ow.set_context(
                            service=self.service,
                            admission_id=self.admission_id,
                            shift_date=self._current_date,
                        )
                    else:
                        ow.service = self.service
                        ow.admission_id = self.admission_id
                        ow.shift_date = self._current_date
                    if getattr(ow, "main_layout", None) is None:
                        ow.setup_ui()
                    if self._is_orders_tab_active():
                        ow.ensure_ready_for_show()
                finally:
                    ow.blockSignals(False)
                self.controls.set_save_active(ow.has_drafts())
                self.controls.set_rollback_active(ow.has_drafts())
                self.controls.set_clean_active(ow.has_administrations())
                self.controls.set_clear_active(ow.has_orders())
            self._apply_archive_read_only_state()
        except Exception as e:
            logger.error(f"[RELOAD] Error during force_reload_all: {e}", exc_info=True)
            raise
        self._update_yesterday_button_state()

    def _update_yesterday_button_state(self):
        if not hasattr(self, 'controls'): return
        now = datetime.now()
        current_start, current_end = self.service.get_day_period(now)
        is_today = current_start <= self._current_date < current_end
        self.controls.set_yesterday_active(is_today)

    def on_out_values_changed(self, new_total_out):
        self.update_balance_data()

    def update_balance_data(self):
        self._ensure_card_widgets_initialized()
        self._bind_balance_widgets_if_ready()
        if self.admission_id is None: return
        if self._balance_calculator_cls is None:
            from ...services.balance_calculator import BalanceCalculator

            self._balance_calculator_cls = BalanceCalculator
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
        if hasattr(self.service, "get_oral_intake_totals"):
            try:
                oral_totals = self.service.get_oral_intake_totals(
                    self.admission_id,
                    self._current_date,
                    current_time=calc_time,
                )
                oral_cur = oral_totals.get("current", 0) or 0
                oral_day = oral_totals.get("daily", 0) or 0
            except Exception as exc:
                logger.warning("Failed to load oral intake totals for balance: %s", exc)
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

    def on_tab_changed(self, tab_name):
        self.layout_manager.set_active_tab(tab_name)
        if tab_name == "Баланс жидкости":
            self._ensure_balance_tab_ready()
        elif tab_name == "Назначения":
            ow = self.layout_manager.orders_widget
            if hasattr(ow, "set_context"):
                ow.set_context(
                    service=self.service,
                    admission_id=self.admission_id,
                    shift_date=self._current_date,
                )
            else:
                ow.service = self.service
                ow.admission_id = self.admission_id
                ow.shift_date = self._current_date
            ow.ensure_ready_for_show()

            is_draft = ow.has_drafts()

            # Проверяем статусы кнопок управления
            self.controls.set_save_active(is_draft)
            self.controls.set_rollback_active(is_draft)
            self.controls.set_clean_active(ow.has_administrations())
            self.controls.set_clear_active(ow.has_orders())
        self._apply_archive_read_only_state()

    def on_clean_sheet_clicked(self):
        if self._archive_read_only_mode:
            self._show_read_only_hint()
            return
        ow = self.layout_manager.orders_widget if hasattr(self.layout_manager, 'orders_widget') else None
        if not ow: return
        reply = ow._show_question("Вы уверены, что хотите очистить текущий лист назначений (удалить все введения за смену)?")
        if reply == CustomMessageBox.Yes: ow.clear_all_times()

    def on_rollback_clicked(self):
        if self._archive_read_only_mode:
            self._show_read_only_hint()
            return
        ow = self.layout_manager.orders_widget if hasattr(self.layout_manager, 'orders_widget') else None
        if not ow: return
        reply = ow._show_question("Вы уверены, что хотите отменить текущие несохраненные изменения?")
        if reply == CustomMessageBox.Yes: ow.clear_drafts()

    def on_exit_clicked(self):
        reply = CustomMessageBox.question(self, "Подтверждение", "Выйти из программы?", CustomMessageBox.Yes | CustomMessageBox.No, CustomMessageBox.No)
        if reply == CustomMessageBox.Yes: self.window().close()

    def on_back_clicked(self):
        self._balance_update_timer.stop()
        current_idx = self.layout_manager.selection_stack.currentIndex()
        journal_idx = -1
        if hasattr(self.layout_manager, "journal_view"):
            journal_idx = self.layout_manager.selection_stack.indexOf(self.layout_manager.journal_view)
        was_journal_mode = (current_idx == journal_idx and journal_idx != -1)

        if current_idx == 0:
            if hasattr(self.layout_manager, 'orders_widget') and not self._archive_read_only_mode:
                self.layout_manager.orders_widget.clear_drafts()
            self.admission_id = None
            self._release_add_patient_lock()
            self._exit_archive_read_only_mode()
            self.layout_manager.set_patient_selection_mode("beds")
            self.layout_manager.bottom_row.show()
            if was_journal_mode:
                self._force_beds_refresh_after_journal_exit()
        elif current_idx in (2, 3, 4):
            # Явно снимаем lock перед выходом из журнала/режимов выбора.
            self._release_add_patient_lock()
            self._exit_archive_read_only_mode()
            self.layout_manager.set_patient_selection_mode("beds")
            self.layout_manager.bottom_row.show()
            if was_journal_mode:
                self._force_beds_refresh_after_journal_exit()
        else:
            self._release_add_patient_lock()
            self._exit_archive_read_only_mode()
            self.back_to_roles_requested.emit()

    def on_settings_clicked(self):
        self._exit_archive_read_only_mode()
        self.layout_manager.set_patient_selection_mode("admin")
        self._wire_dynamic_views()
        self.layout_manager.bottom_row.hide()
        admin_widget = getattr(self.layout_manager, "admin_widget", None)
        if admin_widget:
            admin_widget.set_print_context(self.service, self.admission_id, self._current_date)

    def on_add_patient_clicked(self):
        if self._archive_read_only_mode:
            self._show_read_only_hint()
            return
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
            self.layout_manager.set_patient_selection_mode("journal")
            self.layout_manager.bottom_row.hide()
        except Exception:
            self._release_add_patient_lock()
            raise

    def on_refresh_beds_clicked(self):
        self.force_refresh_everywhere()
        self.refresh_requested.emit()
        if hasattr(self, 'chart'):
            self.chart.update()

    def force_refresh_everywhere(self):
        """Принудительно обновляет максимум доступных представлений без изменения UI-структуры."""
        data_service = self._get_data_service()
        if data_service:
            data_service.request_immediate_refresh(force_emit=True)

        try:
            if hasattr(self.layout_manager, "beds_selection_widget") and self.layout_manager.beds_selection_widget:
                self.layout_manager.beds_selection_widget.refresh()
        except Exception as exc:
            logger.warning("Force refresh: beds list refresh failed: %s", exc)

        try:
            if hasattr(self.layout_manager, "_refresh_archive_if_needed"):
                self.layout_manager._refresh_archive_if_needed(force=True)
        except Exception as exc:
            logger.warning("Force refresh: archive refresh failed: %s", exc)

        current_idx = -1
        if hasattr(self.layout_manager, "selection_stack"):
            current_idx = int(self.layout_manager.selection_stack.currentIndex())
        is_card_mode = bool(self.admission_id) and (current_idx in (-1, 0))

        if is_card_mode:
            try:
                self._request_card_snapshot(
                    ensure_initial_status=not self._archive_read_only_mode,
                    show_empty_message=False,
                    force_emit=True,
                )

                if hasattr(self.layout_manager, "orders_widget") and self.layout_manager.orders_widget:
                    ow = self.layout_manager.orders_widget
                    ow.request_refresh(force=True)

                if hasattr(self.layout_manager, "nurse_orders_manager") and self.layout_manager.nurse_orders_manager:
                    self.layout_manager.nurse_orders_manager.refresh_data()

                events_sector = None
                if hasattr(self.layout_manager, "ensure_events_sector"):
                    events_sector = self.layout_manager.ensure_events_sector()
                else:
                    events_sector = getattr(self.layout_manager, "sector_events", None)
                if events_sector:
                    events_sector.refresh()
            except Exception as exc:
                logger.warning("Force refresh: card mode refresh failed: %s", exc, exc_info=True)

        try:
            if hasattr(self.layout_manager, "journal_widget") and self.layout_manager.journal_widget:
                jw = self.layout_manager.journal_widget
                if hasattr(jw, "refresh_data"):
                    jw.refresh_data()
                if hasattr(jw, "refresh_bed_statuses"):
                    jw.refresh_bed_statuses()
        except Exception as exc:
            logger.warning("Force refresh: journal refresh failed: %s", exc)

    def on_calculator_clicked(self):
        from .components.infusion_calculator import InfusionCalculatorDialog
        # Чистый запуск без передачи веса пациента (калькулятор стартует с 0)
        dialog = InfusionCalculatorDialog(parent=self)
        dialog.exec()

    def on_bonus_clicked(self):
        try:
            from rem_card.app.runtime_paths import resolve_baza_dir
            from rem_card.ui.shared.minigames.bonus_dialog import BonusDialog

            dialog = BonusDialog(role="doctor", data_root_provider=resolve_baza_dir, parent=self)
            dialog.exec()
        except Exception as exc:
            CustomMessageBox.warning(self, "Бонус", f"Не удалось открыть бонус:\n{exc}")

    def on_global_archive_clicked(self):
        self._exit_archive_read_only_mode()
        self.layout_manager.set_patient_selection_mode("archive")
        self._wire_dynamic_views()
        self.layout_manager.bottom_row.hide()

    def on_patient_selected_from_archive(self, patient):
        if getattr(patient, "is_external_archive", False):
            source_db_path = getattr(patient, "source_db_path", None)
            source_admission_id = getattr(patient, "source_admission_id", None)
            if source_admission_id is None:
                source_admission_id = patient.id

            try:
                self._enter_archive_read_only_mode(source_db_path)
                target_date = self._resolve_archive_open_date(int(source_admission_id), fallback_patient=patient)
                self.load_patient_card(int(source_admission_id), target_date)
                self.layout_manager.set_patient_selection_mode("card")
                self.layout_manager.bottom_row.show()
                db_name = getattr(patient, "source_db_name", None) or "архивная БД"
                CustomMessageBox.information(
                    self,
                    "Архивный цикл",
                    f"Карта открыта из {db_name} в режиме только чтения.",
                )
            except Exception as exc:
                logger.error("Failed to open external archived card: %s", exc, exc_info=True)
                CustomMessageBox.warning(self, "Ошибка", f"Не удалось открыть архивную карту:\n{exc}")
                self._exit_archive_read_only_mode()
            return
        self._exit_archive_read_only_mode()
        target_date = datetime.now()
        self.load_patient_card(patient.id, target_date)
        self._prime_patient_header_from_w1(patient, target_date)
        self.layout_manager.set_patient_selection_mode("card")
        self.layout_manager.bottom_row.show()

    def on_patient_selected_from_list(self, patient, action_type):
        self._exit_archive_read_only_mode()
        logger.info(
            "[DOCTOR_VIEW] W1 action requested admission_id=%s action=%s",
            getattr(patient, "id", None),
            action_type,
        )
        if action_type == "show":
            target_date = datetime.now()
            self.load_patient_card(patient.id, target_date)
            self._prime_patient_header_from_w1(patient, target_date)
            self.layout_manager.set_patient_selection_mode("card")
        elif action_type == "create":
            target_date = datetime.now()
            self.load_patient_card(patient.id, target_date, request_snapshot=False)
            self._prime_patient_header_from_w1(patient, target_date)
            self.layout_manager.set_patient_selection_mode("card")
            self.on_create_card_clicked()
        elif action_type == "yest":
            from datetime import timedelta
            yest = datetime.now() - timedelta(days=1)
            QTimer.singleShot(
                0,
                lambda target_patient=patient, target_date=yest: self._open_w1_yesterday_card(
                    target_patient,
                    target_date,
                ),
            )
        elif action_type == "archive":
            self.show_archive(patient)

    def _open_w1_yesterday_card(self, patient, target_date):
        if self._is_closing:
            return
        logger.info(
            "[DOCTOR_VIEW] W1 yesterday card load admission_id=%s target_date=%s",
            getattr(patient, "id", None),
            target_date.isoformat() if hasattr(target_date, "isoformat") else target_date,
        )
        self.load_patient_card(patient.id, target_date, ensure_initial_status=False)
        self._prime_patient_header_from_w1(patient, target_date)
        self.layout_manager.set_patient_selection_mode("card")

    def on_clear_orders_clicked(self):
        if self._archive_read_only_mode:
            self._show_read_only_hint()
            return
        ow = self.layout_manager.orders_widget if hasattr(self.layout_manager, 'orders_widget') else None
        if not ow: return
        reply = ow._show_question("Уверены ли вы, что необходимо очистить назначения (удалить все препараты)?")
        if reply == CustomMessageBox.Yes: ow.clear_all_orders()

    def on_daily_report_clicked(self):
        """Обработка запроса отчета за сутки из открытой карты пациента."""
        self.status_label_info = "Сборка отчета за сутки..."
        controller = self._get_report_controller()
        controller.run_daily_report(self.admission_id, self._current_date)
        # Совместимость: сохраняем ссылку на воркер на уровне виджета.
        self.daily_worker = controller.daily_worker

    def on_full_report_clicked(self):
        """Обработка запроса общего отчета из открытой карты пациента."""
        self.status_label_info = "Сборка отчета..."
        controller = self._get_report_controller()
        controller.run_full_report(self.admission_id)
        # Совместимость: сохраняем ссылку на воркер на уровне виджета.
        self.report_worker = controller.full_worker

    def update_latest_indicators(self):
        if not hasattr(self.layout_manager, 'sector_4v') or self.admission_id is None: return

        try:
            snapshot = self._card_snapshot_cache or {}
            latest_values = snapshot.get("latest_values") or {}
            settings = snapshot.get("settings") or {}
            
            if hasattr(self.layout_manager, 'sector_2g'):
                self.layout_manager.sector_2g.update_legend(settings)
                
            self.layout_manager.sector_4v.update_latest_vitals(latest_values, settings)
            self.layout_manager.sector_4v.update()
        except Exception as e:
            from ...app.logger import logger
            logger.error(f"Error updating latest indicators: {e}")

    def update_patient_info(self):
        try:
            snapshot = self._card_snapshot_cache or {}
            patient = snapshot.get("patient")
            if patient and hasattr(self.layout_manager, 'sector_4b'):
                self.layout_manager.sector_4b.update_patient_info(patient, self._current_date)
            if hasattr(self.layout_manager, 'sector_4v'):
                card_exists = bool(snapshot.get("card_exists"))
                yest_exists = bool(snapshot.get("yest_exists"))
                self.layout_manager.sector_4v.set_buttons_state(card_exists, yest_exists)
                self.update_latest_indicators()
                self._apply_archive_read_only_state()
            if hasattr(self.layout_manager, "set_current_status_dto"):
                self.layout_manager.set_current_status_dto(snapshot.get("status"))
            self.layout_manager.refresh_current_status()
        except Exception as e:
            from ...app.logger import logger
            logger.error(f"Error updating patient info in sector 4b/4v: {e}")

    def refresh_data(self, show_empty_message=False):
        self._ensure_card_widgets_initialized()
        self._request_card_snapshot(show_empty_message=show_empty_message)

    def closeEvent(self, event):
        self._is_closing = True
        self._balance_update_timer.stop()
        if hasattr(self, "_add_patient_lock_watch_timer"):
            self._add_patient_lock_watch_timer.stop()
        self._disconnect_monitor()
        self._release_add_patient_lock()
        if hasattr(self.layout_manager, 'orders_widget') and not self._archive_read_only_mode:
            self.layout_manager.orders_widget.clear_drafts()
        self._close_archive_readonly_manager()
        if event is not None: super().closeEvent(event)

