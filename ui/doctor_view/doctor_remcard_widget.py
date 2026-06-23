import json
import os
import socket
import time
from rem_card.ui.shared.async_call import AsyncCallThread
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.shared.loading_overlay import hide_app_loading, show_app_loading
from PySide6.QtWidgets import QWidget, QVBoxLayout, QDialog, QStackedWidget
from PySide6.QtCore import Signal, Qt, QTimer
from datetime import datetime, timedelta
from rem_card.app.foreground_activity import mark_foreground_activity, should_defer_background_io
from rem_card.app.logger import logger
from rem_card.app.local_metrics import record_metric
from rem_card.app.paths import get_role_lock_path
from rem_card.app.role_session_lock import RoleSessionLock
from rem_card.services.archive_readonly_service import create_archive_readonly_service
from rem_card.ui.shared.orders_balance_adapter import (
    apply_current_order_mark_overrides,
    build_balance_orders_from_orders_widget,
    oral_totals_from_runtime,
)

ADD_PATIENT_LOCK_POLL_INTERVAL_MS = 1500
ADD_PATIENT_LOCK_KEY = "add_patient_button"
PATIENT_BED_MANAGEMENT_MODE = "patient_bed_management"
CARD_UI_PREWARM_ENABLED = os.environ.get("REMCARD_CARD_UI_PREWARM", "0") == "1"
CARD_UI_PREWARM_DELAY_MS = max(0, int(os.environ.get("REMCARD_CARD_PREWARM_DELAY_MS", "900")))
CARD_UI_PREWARM_STAGGER_MS = max(0, int(os.environ.get("REMCARD_CARD_PREWARM_STAGGER_MS", "120")))
CARD_OPEN_HYDRATE_DELAY_MS = max(0, int(os.environ.get("REMCARD_CARD_OPEN_HYDRATE_DELAY_MS", "250")))
CARD_HYDRATION_FOREGROUND_IDLE_SEC = max(
    0.0,
    float(os.environ.get("REMCARD_CARD_HYDRATION_FOREGROUND_IDLE_SEC", "3")),
)
CARD_HYDRATION_MAX_DEFER_ATTEMPTS = max(
    0,
    int(os.environ.get("REMCARD_CARD_HYDRATION_MAX_DEFER_ATTEMPTS", "5")),
)
CHART_LAZY_INIT_DELAY_MS = max(0, int(os.environ.get("REMCARD_CHART_LAZY_INIT_DELAY_MS", "0")))
JOURNAL_PREWARM_DELAY_MS = max(0, int(os.environ.get("REMCARD_JOURNAL_PREWARM_DELAY_MS", "60000")))
JOURNAL_PREWARM_ENABLED = os.environ.get("REMCARD_JOURNAL_PREWARM", "0") == "1"
JOURNAL_WIDGET_PREWARM_ENABLED = os.environ.get("REMCARD_JOURNAL_WIDGET_PREWARM", "0") == "1"
LOCAL_ORDER_FORCE_PREFIXES = (
    "orders_add_input:",
    "orders_add_cvp:",
    "orders_edit_input:",
    "orders_left_click:",
    "orders_middle_click:",
    "orders_right_click:",
    "doctor_order_mark:",
    "nurse_order_mark:",
    "nurse_order_panel_mark:",
)
EMERGENCY_NOTICE_FORCE_PREFIX = "emergency_notice_save:"
ORDER_CHANGE_ENTITIES = {"orders", "administrations"}
LAB_ORDER_CHANGE_ENTITIES = {"lab_orders"}
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

class DoctorRemCardWidget(QWidget):
    archive_requested = Signal()
    back_to_roles_requested = Signal()
    refresh_requested = Signal()

    def __init__(self, remcard_service, admission_id, patient_service=None, parent=None, operblock_service=None):
        super().__init__(parent)
        self._primary_service = remcard_service
        self.service = remcard_service
        self.admission_id = admission_id
        self.patient_service = patient_service
        self.operblock_service = operblock_service
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
        self._orders_widget_signals_bound = False
        self._nurse_orders_balance_signals_bound = False
        self.report_controller = None
        self._card_ui_prewarm_started = False
        self._card_ui_prewarm_done = False
        self._chart_init_pending = False
        self._last_applied_card_snapshot_signature = None
        self._last_applied_chart_signature = None
        self._journal_prewarm_started = False
        self._journal_prewarm_done = False
        self._selection_mode = "beds"
        self._settings_return_mode = None
        self._card_return_mode = None
        self._card_opened_from_global_archive = False
        self._archive_read_only_mode = False
        self._archive_source_db_path = None
        self._archive_readonly_db_manager = None
        self._balance_patient_period_manual_mode = False
        self._snapshot_worker = None
        self._snapshot_pending = None
        self._snapshot_request_id = 0
        self._create_card_after_snapshot = False
        self._create_card_write_pending = False
        self._monitor_connected = False
        self._card_snapshot_cache = None
        self._balance_runtime_cache = None
        self._read_only_widget_signature = None
        self._operblock_archive_viewer = None
        self._operblock_archive_db_manager = None
        self._operblock_archive_source_db_path = None
        self._is_closing = False
        self.diet_intake_widget = None
        self._full_layout_created = False
        self._full_layout_static_signals_bound = False
        self._patient_open_generation = 0
        self._last_plan_card_open_state = False
        self._add_patient_lock = self._build_add_patient_lock()
        self._add_patient_lock_held = False
        self._add_patient_locked_by_other = False
        self._bars_auth_service = None
        
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
        """Локально обновляет список коек сразу после выхода из управления пациентами."""
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
            self._set_add_patient_button_hint("Открыть управление пациентами")
        else:
            self._set_add_patient_button_hint("Кнопка доступна только в режиме списка коек")

    def _refresh_add_patient_button_lock_state(self):
        try:
            if self._is_closing or not self._is_qobject_alive(self):
                return
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

    def _get_cached_patient_vitals_snapshot(self, admission_id, shift_date):
        if self._archive_read_only_mode:
            return None
        coordinator = self._get_read_coordinator()
        if coordinator is None or not admission_id or shift_date is None:
            return None
        try:
            context = coordinator.make_patient_snapshot_context(
                source_db="live",
                admission_id=int(admission_id),
                shift_date=shift_date,
                role="doctor",
                mode="live",
                variant="vitals",
            )
            if hasattr(coordinator, "get_cached_vitals"):
                return coordinator.get_cached_vitals(context.cache_key())
            if hasattr(coordinator, "get_current_cached_vitals"):
                return coordinator.get_current_cached_vitals(context.cache_key())
        except Exception as exc:
            logger.debug("Doctor vitals cache lookup failed: %s", exc)
            return None

    def _get_cached_patient_card_snapshot(self, admission_id, shift_date):
        if self._archive_read_only_mode:
            return None
        coordinator = self._get_read_coordinator()
        if coordinator is None or not admission_id or shift_date is None:
            return None
        try:
            context = coordinator.make_patient_snapshot_context(
                source_db="live",
                admission_id=int(admission_id),
                shift_date=shift_date,
                role="doctor",
                mode="live",
                variant="card_full",
            )
            if hasattr(coordinator, "get_cached_card"):
                return coordinator.get_cached_card(context.cache_key())
        except Exception as exc:
            logger.debug("Doctor card cache lookup failed: %s", exc)
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
            "show_empty_message": False,
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
            "DoctorRemCardWidget applied cached vitals snapshot admission_id=%s version=%s",
            admission_id,
            snapshot.get("version"),
        )
        return True

    @staticmethod
    def _card_snapshot_apply_signature(snapshot: dict):
        if not snapshot:
            return None
        content_hash = snapshot.get("content_hash")
        cache_key = snapshot.get("cache_key")
        if cache_key is not None and content_hash:
            try:
                version = int(snapshot.get("version") or snapshot.get("change_id") or 0)
            except Exception:
                version = 0
            return (
                cache_key,
                str(snapshot.get("scope") or ""),
                version,
                str(content_hash),
            )
        dedup_signature = snapshot.get("dedup_signature")
        if dedup_signature is not None:
            return ("dedup", tuple(dedup_signature))
        return None

    def _chart_matches_context(self, admission_id, start_dt):
        chart = getattr(self, "chart", None)
        if chart is None:
            return False
        return (
            int(getattr(chart, "admission_id", 0) or 0) == int(admission_id or 0)
            and getattr(chart, "start_time", None) == start_dt
            and bool(getattr(chart, "vitals_data", None))
        )

    def _chart_snapshot_signature(self, snapshot: dict):
        chart = getattr(self, "chart", None)
        if not snapshot or chart is None:
            return None
        chart_cls = chart.__class__
        normalize_dt = getattr(chart_cls, "_normalize_key_dt", None)
        build_vitals_key = getattr(chart_cls, "_build_vitals_key", None)
        build_intervals_key = getattr(chart_cls, "_build_intervals_key", None)
        if not (normalize_dt and build_vitals_key and build_intervals_key):
            return None
        runtime = snapshot.get("balance_runtime") or {}
        active_intervals = snapshot.get("chart_active_intervals") or runtime.get("active_intervals")
        try:
            return (
                int(snapshot.get("admission_id") or self.admission_id or 0),
                normalize_dt(snapshot.get("start_dt")),
                build_vitals_key(snapshot.get("vitals_extended") or []),
                build_intervals_key(active_intervals or []),
            )
        except Exception as exc:
            logger.debug("Doctor chart snapshot signature failed: %s", exc)
            return None

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

    def _schedule_card_hydration_snapshot(
        self,
        admission_id: int,
        shift_date: datetime,
        *,
        ensure_initial_status: bool,
    ):
        context_key = self._current_snapshot_context_key(
            admission_id=admission_id,
            shift_date=shift_date,
            load_scope="patient_open_card",
        )
        QTimer.singleShot(
            CARD_OPEN_HYDRATE_DELAY_MS,
            lambda: self._request_card_hydration_if_current(
                admission_id,
                shift_date,
                context_key,
                ensure_initial_status=ensure_initial_status,
            ),
        )

    def _request_card_hydration_if_current(
        self,
        admission_id: int,
        shift_date: datetime,
        context_key,
        *,
        ensure_initial_status: bool,
        defer_attempts: int = 0,
    ):
        if int(admission_id or 0) != int(self.admission_id or 0):
            return
        if shift_date != self._current_date:
            return
        if context_key != self._current_snapshot_context_key(load_scope="patient_open_card"):
            return

        should_defer, reason, age_sec = should_defer_background_io(
            idle_window_sec=CARD_HYDRATION_FOREGROUND_IDLE_SEC,
            names={"orders", "orders_show"},
        )
        active_foreground = str(reason or "").startswith("active:")
        if should_defer and (active_foreground or defer_attempts < CARD_HYDRATION_MAX_DEFER_ATTEMPTS):
            delay_ms = max(1000, CARD_OPEN_HYDRATE_DELAY_MS)
            logger.info(
                "[DOCTOR_VIEW] card_hydration_deferred_for_foreground admission_id=%s reason=%s age_sec=%s attempt=%s delay_ms=%s",
                admission_id,
                reason,
                None if age_sec is None else round(age_sec, 3),
                defer_attempts + 1,
                delay_ms,
            )
            record_metric(
                "card_hydration_deferred_for_foreground",
                1,
                admission_id=admission_id,
                reason=reason,
                age_sec=None if age_sec is None else round(age_sec, 3),
                attempt=defer_attempts + 1,
                source="refresh",
            )
            QTimer.singleShot(
                delay_ms,
                lambda: self._request_card_hydration_if_current(
                    admission_id,
                    shift_date,
                    context_key,
                    ensure_initial_status=ensure_initial_status,
                    defer_attempts=defer_attempts + 1,
                ),
            )
            return

        self._request_card_snapshot(
            ensure_initial_status=ensure_initial_status,
            show_empty_message=False,
            load_scope="patient_open_card",
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

    def _disconnect_snapshot_worker(self, worker):
        if worker is None:
            return
        for signal, slot in (
            (worker.succeeded, self._apply_card_snapshot),
            (worker.failed, self._on_card_snapshot_failed),
            (worker.finished, self._on_card_snapshot_finished),
        ):
            try:
                signal.disconnect(slot)
            except Exception:
                pass

    def _shutdown_snapshot_worker(self, timeout_ms: int = 1200):
        self._snapshot_pending = None
        self._create_card_after_snapshot = False
        worker = self._snapshot_worker
        self._snapshot_worker = None
        if worker is None:
            return
        self._disconnect_snapshot_worker(worker)
        if worker.isRunning():
            worker.quit()
            worker.wait(timeout_ms)

    def _request_pending_card_snapshot(self):
        if self._is_closing:
            self._snapshot_pending = None
            return
        pending = self._snapshot_pending
        self._snapshot_pending = None
        if not pending:
            return
        self._request_card_snapshot(
            ensure_initial_status=pending["ensure_initial_status"],
            show_empty_message=pending["show_empty_message"],
            load_scope=pending.get("load_scope", "full"),
        )

    def _request_card_snapshot(
        self,
        *,
        ensure_initial_status: bool = False,
        show_empty_message: bool = False,
        force_emit: bool = False,
        load_scope: str = "full",
    ):
        if self._is_closing:
            return
        if not self.admission_id:
            return
        ensure_initial_status = bool(ensure_initial_status) and self._should_ensure_initial_status_for_date(
            self._current_date
        )

        if self._snapshot_worker is not None:
            self._snapshot_pending = {
                "ensure_initial_status": ensure_initial_status,
                "show_empty_message": bool(show_empty_message),
                "load_scope": str(load_scope or "full"),
            }
            return

        self._snapshot_request_id += 1
        request = {
            "request_id": self._snapshot_request_id,
            "admission_id": int(self.admission_id),
            "shift_date": self._current_date,
            "ensure_initial_status": ensure_initial_status,
            "show_empty_message": bool(show_empty_message),
            "load_scope": str(load_scope or "full"),
            "context_key": self._current_snapshot_context_key(load_scope=load_scope),
        }

        worker = AsyncCallThread(self._build_card_snapshot_job, request)
        self._snapshot_worker = worker
        worker.succeeded.connect(self._apply_card_snapshot)
        worker.failed.connect(self._on_card_snapshot_failed)
        worker.finished.connect(self._on_card_snapshot_finished)
        message = (
            "Загрузка карты пациента..."
            if str(load_scope or "").startswith("patient_open")
            else "Обновление данных карты..."
        )
        show_app_loading(
            self,
            message,
            key=f"doctor-card-snapshot:{id(self)}",
            auto_hide_ms=20000,
        )
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
                    force_refresh=False,
                )
            else:
                logger.warning(
                    "DoctorRemCardWidget: ReadCoordinator unavailable, using build_full_card_snapshot for patient open"
                )
                snapshot = self.service.build_full_card_snapshot(
                    request["admission_id"],
                    request["shift_date"],
                    include_change_cursor=True,
                    include_balance=True,
                    balance_only_committed=False,
                    ensure_initial_status=request["ensure_initial_status"],
                )
        elif load_scope in {"patient_open_card", "full"}:
            coordinator = self._get_read_coordinator()
            if coordinator is not None and hasattr(coordinator, "load_patient_card_snapshot"):
                snapshot = coordinator.load_patient_card_snapshot(
                    request["admission_id"],
                    request["shift_date"],
                    role="doctor",
                    mode="archive" if self._archive_read_only_mode else "live",
                    source_db=self._archive_source_db_path if self._archive_read_only_mode else "live",
                    ensure_initial_status=request["ensure_initial_status"],
                    balance_only_committed=False,
                    force_refresh=False,
                )
            else:
                snapshot = self.service.build_full_card_snapshot(
                    request["admission_id"],
                    request["shift_date"],
                    include_change_cursor=True,
                    include_balance=True,
                    balance_only_committed=False,
                    ensure_initial_status=request["ensure_initial_status"],
                )
        else:
            snapshot = self.service.build_full_card_snapshot(
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
        if self._is_closing:
            return
        request_id = request.get("request_id")
        if request_id is None and not request.get("from_cache"):
            logger.info(
                "DoctorRemCardWidget discarded snapshot without request_id current_request_id=%s",
                self._snapshot_request_id,
            )
            return
        if request_id is not None and request_id != self._snapshot_request_id:
            logger.info(
                "DoctorRemCardWidget discarded stale snapshot request_id=%s current_request_id=%s",
                request_id,
                self._snapshot_request_id,
            )
            return
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
        previous_snapshot = self._card_snapshot_cache or {}
        snapshot_signature = self._card_snapshot_apply_signature(snapshot)
        if (
            snapshot_signature is not None
            and snapshot_signature == self._last_applied_card_snapshot_signature
        ):
            self._card_snapshot_cache = snapshot
            self._balance_runtime_cache = snapshot.get("balance_runtime")
            self._last_change_id = max(
                int(self._last_change_id or 0),
                int(snapshot.get("change_id") or 0),
            )
            logger.info(
                "DoctorRemCardWidget skipped unchanged card snapshot admission_id=%s scope=%s version=%s",
                request.get("admission_id"),
                snapshot.get("scope"),
                snapshot.get("version"),
            )
            return
        if (
            previous_snapshot
            and not request.get("from_cache")
            and previous_snapshot.get("cache_key") == snapshot.get("cache_key")
            and int(previous_snapshot.get("version") or 0) == int(snapshot.get("version") or 0)
            and previous_snapshot.get("scope") == snapshot.get("scope")
            and previous_snapshot.get("load_trace_id") == snapshot.get("load_trace_id")
        ):
            logger.info(
                "DoctorRemCardWidget skipped unchanged cached snapshot admission_id=%s scope=%s version=%s",
                request.get("admission_id"),
                snapshot.get("scope"),
                snapshot.get("version"),
            )
            return
        self._card_snapshot_cache = snapshot
        self._last_applied_card_snapshot_signature = snapshot_signature
        self._balance_runtime_cache = snapshot.get("balance_runtime")
        effective_bounds = snapshot.get("effective_bounds")

        self._ensure_card_widgets_initialized()
        self._bind_balance_widgets_if_ready()

        if hasattr(self, "chart"):
            self._update_chart_from_snapshot(snapshot)
        else:
            self._schedule_chart_init()

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
        self._update_emergency_notice_sector(snapshot)
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
        if self._is_closing:
            return
        exc_info = (type(exc), exc, exc.__traceback__) if isinstance(exc, BaseException) else None
        logger.error("DoctorRemCardWidget snapshot load failed: %s", exc, exc_info=exc_info)

    def _on_card_snapshot_finished(self):
        worker = self.sender()
        if self._snapshot_worker is worker:
            self._snapshot_worker = None
        elif self._snapshot_worker is not None:
            return
        if self._is_closing:
            self._snapshot_pending = None
            self._create_card_after_snapshot = False
            return
        pending_create = self._create_card_after_snapshot
        if pending_create:
            self._create_card_after_snapshot = False
            self._snapshot_pending = None
            hide_app_loading(self, f"doctor-card-snapshot:{id(self)}", delay_ms=350)
            if isinstance(pending_create, dict):
                QTimer.singleShot(0, lambda data=pending_create: self.on_create_card_clicked(**data))
            else:
                QTimer.singleShot(0, self.on_create_card_clicked)
            return
        if self._snapshot_pending:
            QTimer.singleShot(0, self._request_pending_card_snapshot)
            return
        hide_app_loading(self, f"doctor-card-snapshot:{id(self)}", delay_ms=350)

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
        try:
            self._ensure_card_widgets_initialized()
            self._bind_balance_widgets_if_ready()
            if not self.admission_id:
                return
            snapshot = None
            coordinator = self._get_read_coordinator()
            if coordinator is not None and hasattr(coordinator, "load_balance_snapshot"):
                snapshot = coordinator.load_balance_snapshot(
                    self.admission_id,
                    self._current_date,
                    role="doctor",
                    mode="archive" if self._archive_read_only_mode else "live",
                    source_db=self._archive_source_db_path if self._archive_read_only_mode else "live",
                    balance_only_committed=False,
                    force_refresh=True,
                )
            elif hasattr(self.service, "build_balance_snapshot"):
                snapshot = self.service.build_balance_snapshot(
                    self.admission_id,
                    self._current_date,
                    include_change_cursor=True,
                    balance_only_committed=False,
                )

            if snapshot:
                cached_snapshot = dict(self._card_snapshot_cache or {})
                for key in ("effective_bounds", "fluids", "balance_runtime", "balance_calc", "change_id", "version"):
                    if key in snapshot:
                        cached_snapshot[key] = snapshot.get(key)
                self._card_snapshot_cache = cached_snapshot
                if snapshot.get("balance_runtime") is not None:
                    self._balance_runtime_cache = snapshot.get("balance_runtime")
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
                return

            if hasattr(self, "balance_controller"):
                self.balance_controller.refresh()
            else:
                self.update_balance_data()
        except Exception:
            logger.exception("Doctor balance partial refresh failed")

    def _refresh_status_from_db(self) -> None:
        try:
            if hasattr(self.layout_manager, "set_current_status_dto"):
                self.layout_manager.set_current_status_dto(None)
            if hasattr(self.layout_manager, "refresh_current_status"):
                self.layout_manager.refresh_current_status()
            events_sector = getattr(self.layout_manager, "sector_events", None)
            if events_sector is not None and hasattr(events_sector, "refresh"):
                events_sector.refresh(force=True)
        except Exception:
            logger.exception("Doctor status partial refresh failed")

    def _refresh_ivl_from_db(self) -> None:
        try:
            sector_ivl = getattr(self.layout_manager, "sector_ivl", None)
            if sector_ivl is not None and hasattr(sector_ivl, "refresh"):
                sector_ivl.refresh()
        except Exception:
            logger.exception("Doctor IVL partial refresh failed")

    def _refresh_procedures_from_db(self) -> None:
        try:
            sector_proc = getattr(self.layout_manager, "sector_proc", None)
            if sector_proc is not None and hasattr(sector_proc, "refresh"):
                sector_proc.refresh()
        except Exception:
            logger.exception("Doctor procedures partial refresh failed")

    def _sync_lab_orders_context(self) -> bool:
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
                if hasattr(sector_anal, "set_lab_orders"):
                    sector_anal.set_lab_orders([])
                return True
            if hasattr(sector_anal, "set_context"):
                sector_anal.set_context(self.service, self.admission_id, self._current_date)
                return True
            if hasattr(sector_anal, "refresh"):
                sector_anal.refresh()
                return True
        except Exception:
            logger.exception("Doctor lab orders context sync failed")
        return False

    def _refresh_labs_from_db(self) -> None:
        try:
            self._sync_lab_orders_context()
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
        diet_entities = {"diet_templates", "diet_plan", "oral_intake_events"}
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
            from ..shared.report_controller import RemCardReportController

            self.report_controller = RemCardReportController(self.service, self)
        return self.report_controller

    def _schedule_balance_update(self, *_args):
        if not self.admission_id:
            return
        self._balance_update_timer.start(self._balance_update_delay_ms)

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
        return (
            list(getattr(widget, "_events", []) or []),
            getattr(widget, "_plan", None),
        )

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
            self.balance_controller.service = service.fluid_service
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
            "Запись прошлых периодов доступна только для просмотра.",
        )

    def _current_status_is_outcome(self) -> bool:
        snapshot = self._card_snapshot_cache or {}
        status_dto = snapshot.get("status")
        status_value = getattr(status_dto, "status", None)
        if status_dto and getattr(status_value, "is_outcome", lambda: False)():
            return True
        layout_status = getattr(getattr(self, "layout_manager", None), "_current_status_dto", None)
        layout_status_value = getattr(layout_status, "status", None)
        if layout_status and getattr(layout_status_value, "is_outcome", lambda: False)():
            return True
        patient = snapshot.get("patient")
        if patient and (
            getattr(patient, "transfer_datetime", None)
            or getattr(patient, "death_datetime", None)
            or getattr(patient, "outcome", None)
        ):
            return True
        if self.admission_id and getattr(self, "service", None) and hasattr(self.service, "get_current_status"):
            try:
                current_status = self.service.get_current_status(self.admission_id)
            except Exception:
                current_status = None
            current_status_value = getattr(current_status, "status", None)
            if current_status and getattr(current_status_value, "is_outcome", lambda: False)():
                return True
        return False

    def _apply_archive_read_only_state(self):
        read_only = bool(self._archive_read_only_mode)
        layout = getattr(self, "layout_manager", None)
        ow = getattr(layout, "orders_widget", None) if layout is not None else None
        events_sector = getattr(layout, "sector_events", None) if layout is not None else None
        emergency_sector = getattr(layout, "sector_7vit_b", None) if layout is not None else None
        diet_widget = getattr(self, "diet_intake_widget", None)
        widget_signature = (
            int(self.admission_id or 0),
            self._current_date.isoformat(timespec="seconds") if self._current_date else None,
            "doctor",
            read_only,
            bool(self._archive_source_db_path),
            id(getattr(self, "vitals_input", None)) if hasattr(self, "vitals_input") else None,
            id(ow) if ow else None,
            id(events_sector) if events_sector else None,
            id(emergency_sector) if emergency_sector else None,
            id(diet_widget) if diet_widget else None,
        )
        apply_widget_state = widget_signature != self._read_only_widget_signature

        if apply_widget_state and hasattr(self, "vitals_input") and self.vitals_input:
            if hasattr(self.vitals_input, "set_forced_read_only"):
                self.vitals_input.set_forced_read_only(read_only)
            else:
                self.vitals_input.setEnabled(not read_only)

        if apply_widget_state and ow:
            if hasattr(ow, "set_forced_read_only"):
                ow.set_forced_read_only(read_only)
            else:
                ow.setEnabled(not read_only)

        if apply_widget_state and events_sector:
            events_sector.setEnabled(not read_only)
        if apply_widget_state and emergency_sector and hasattr(emergency_sector, "set_forced_read_only"):
            emergency_sector.set_forced_read_only(read_only)
        if apply_widget_state and diet_widget:
            diet_widget.set_read_only(read_only)
        if apply_widget_state:
            self._read_only_widget_signature = widget_signature

        if hasattr(self, "controls") and self.controls:
            if read_only:
                self.controls.btn_save.setEnabled(False)
                self.controls.btn_clean_sheet.setEnabled(False)
                self.controls.btn_clear.setEnabled(False)
                self.controls.btn_yesterday.setEnabled(False)
                self.controls.btn_rollback.setEnabled(False)
                self.controls.btn_templates.setEnabled(False)
                self.controls.btn_pokaz.setEnabled(False)
                self._set_lab_yesterday_button_active(False)
            else:
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
            card_exists, yest_exists, plan_card_available = self._sector_4v_button_state(snapshot)

            # Сохраняем бизнес-логику 4в (наличие карт), добавляя только ограничение read-only.
            s4v.set_buttons_state(card_exists, yest_exists, plan_card_available)
            if read_only or self._current_status_is_outcome_safe():
                s4v.btn_new_card.setEnabled(False)
                if hasattr(s4v, "btn_plan_card"):
                    s4v.btn_plan_card.setEnabled(False)
            s4v.btn_card_list.setEnabled(True)
            s4v.btn_daily_print.setEnabled(True)
            s4v.btn_all_print.setEnabled(True)

    def _latest_created_card_date(self, admission_id: int):
        try:
            card_dates = self.service.get_all_card_dates(admission_id)
            if card_dates:
                return max(card_dates)
        except Exception as exc:
            logger.warning("Failed to resolve latest card date in archive DB: %s", exc)
        return None

    def _resolve_archive_open_date(self, admission_id: int, fallback_patient=None) -> datetime:
        latest_date = self._latest_created_card_date(admission_id)
        if latest_date:
            return latest_date
        if fallback_patient and getattr(fallback_patient, "admission_datetime", None):
            return fallback_patient.admission_datetime
        return datetime.now()

    def _is_same_medical_day(self, left: datetime, right: datetime) -> bool:
        try:
            left_start, _ = self.service.get_day_period(left)
            right_start, _ = self.service.get_day_period(right)
            return left_start == right_start
        except Exception:
            return left == right

    def _plan_card_state_for_admission(self, admission_id: int, now: datetime | None = None):
        if not self.service or not hasattr(self.service, "build_plan_card_state"):
            return {}
        try:
            return dict(self.service.build_plan_card_state(int(admission_id), now=now))
        except Exception as exc:
            logger.warning("Failed to resolve planned card state admission_id=%s: %s", admission_id, exc)
            return {}

    def _card_shift_start(self, value: datetime | None):
        if value is None or not self.service or not hasattr(self.service, "get_day_period"):
            return None
        try:
            start, _end = self.service.get_day_period(value)
            return start
        except Exception:
            return None

    def _is_plan_card_date(self, value: datetime | None, now: datetime | None = None) -> bool:
        if value is None or not self.admission_id:
            return False
        reference_dt = now or datetime.now()
        state = self._plan_card_state_for_admission(int(self.admission_id), now=reference_dt)
        if not state.get("plan_card_window_active"):
            return False
        target_date = state.get("plan_card_target_date")
        target_start = self._card_shift_start(target_date)
        value_start = self._card_shift_start(value)
        return bool(target_start is not None and value_start == target_start)

    def _is_plan_card_open(self) -> bool:
        if getattr(self, "_archive_read_only_mode", False):
            return False
        return self._is_plan_card_date(getattr(self, "_current_date", None))

    def _card_button_reference_date(self) -> datetime:
        return datetime.now() if self._is_plan_card_open() else self._current_date

    def _daily_report_reference_date(self) -> datetime:
        return self._card_button_reference_date()

    def daily_report_reference_date(self) -> datetime:
        return self._daily_report_reference_date()

    def _current_status_is_outcome_safe(self) -> bool:
        checker = getattr(self, "_current_status_is_outcome", None)
        return bool(checker()) if callable(checker) else False

    def _sector_4v_button_state(self, snapshot=None) -> tuple[bool, bool, bool]:
        snapshot = snapshot if isinstance(snapshot, dict) else (self._card_snapshot_cache or {})
        if self._is_plan_card_open() and self.admission_id:
            now = datetime.now()
            plan_state = self._plan_card_state_for_admission(int(self.admission_id), now=now)
            try:
                card_exists = bool(self.service.has_card(int(self.admission_id), now))
            except Exception:
                card_exists = bool(plan_state.get("plan_card_available"))
            try:
                current_start, _current_end = self.service.get_day_period(now)
                yest_exists = bool(self.service.has_card(int(self.admission_id), current_start - timedelta(days=1)))
            except Exception:
                yest_exists = bool(snapshot.get("yest_exists"))
            return card_exists, yest_exists, bool(plan_state.get("plan_card_available"))
        return (
            bool(snapshot.get("card_exists")),
            bool(snapshot.get("yest_exists")),
            bool(snapshot.get("plan_card_available")),
        )

    def _sync_plan_card_ui_state(self):
        layout = getattr(self, "layout_manager", None)
        plan_card_open = self._is_plan_card_open()
        if layout is not None and hasattr(layout, "set_plan_card_mode"):
            layout.set_plan_card_mode(plan_card_open)
        previous = bool(getattr(self, "_last_plan_card_open_state", False))
        self._last_plan_card_open_state = bool(plan_card_open)
        return previous != bool(plan_card_open)

    def _should_ensure_initial_status_for_date(self, value: datetime) -> bool:
        if getattr(self, "_archive_read_only_mode", False):
            return False
        try:
            current_start, current_end = self.service.get_day_period(datetime.now())
            return current_start <= value < current_end
        except Exception as exc:
            logger.warning("Failed to resolve current medical day for initial status guard: %s", exc)
            return False

    def load_patient_card(
        self,
        admission_id,
        date,
        *,
        request_snapshot: bool = True,
        ensure_initial_status=None,
        balance_patient_period_manual_mode: bool = False,
    ):
        """Обновляет данные карты для нового пациента/даты."""
        if self._is_closing:
            return
        open_loading_key = show_app_loading(
            self,
            "Открытие карты пациента...",
            key=f"doctor-card-open:{id(self)}",
            auto_hide_ms=10000,
            process_events=True,
        )
        self._patient_open_generation += 1
        patient_open_generation = self._patient_open_generation
        if not self._ensure_full_layout(reason="patient_open"):
            if open_loading_key:
                hide_app_loading(self, open_loading_key, delay_ms=350)
            return
        self._schedule_card_ui_prewarm()
        self._ensure_card_widgets_initialized()
        from rem_card.app.logger import logger
        logger.info(f"[DOCTOR_VIEW] Loading patient card. AdmID: {admission_id}, Date: {date}")

        self._balance_update_timer.stop()

        orders_context_unchanged = self._prepare_patient_card_orders_context(admission_id, date)
        (
            card_start_dt,
            card_end_dt,
            cached_card_snapshot,
            cached_vitals_snapshot,
        ) = self._reset_patient_card_context_state(
            admission_id,
            date,
            balance_patient_period_manual_mode,
        )
        self._sync_patient_card_layout_context(
            admission_id,
            date,
            card_start_dt,
            card_end_dt,
            orders_context_unchanged,
        )
        self._last_change_id = 0
        self._apply_archive_read_only_state()
        if not cached_card_snapshot:
            self._reset_balance_view_state()
        if cached_vitals_snapshot:
            self._apply_patient_open_cache(admission_id, date, cached_vitals_snapshot)
        self._schedule_patient_card_snapshots(
            admission_id,
            date,
            request_snapshot=request_snapshot,
            ensure_initial_status=ensure_initial_status,
            cached_vitals_snapshot=cached_vitals_snapshot,
        )
        self._activate_patient_card_vitals_tab()
        self._sync_patient_card_auxiliary_contexts(admission_id, date)
        self._schedule_nurse_orders_context_for_patient_open(
            admission_id,
            date,
            patient_open_generation,
        )

        # Запуск фонового обновления
        QTimer.singleShot(0, self.start_polling)
        if open_loading_key:
            hide_app_loading(self, open_loading_key, delay_ms=600)

    def _prepare_patient_card_orders_context(self, admission_id, date) -> bool:
        orders_widget = self._ensure_orders_widget()
        orders_context_unchanged = False
        if orders_widget is not None:
            try:
                orders_context_unchanged = (
                    int(getattr(orders_widget, "admission_id", 0) or 0) == int(admission_id or 0)
                    and getattr(orders_widget, "shift_date", None) == date
                )
            except Exception:
                orders_context_unchanged = False
        if orders_widget is not None and not self._archive_read_only_mode and not orders_context_unchanged:
            orders_widget.clear_drafts()
        return orders_context_unchanged

    def _reset_patient_card_context_state(self, admission_id, date, balance_patient_period_manual_mode):
        self.admission_id = admission_id
        self.current_date = date
        self._balance_patient_period_manual_mode = bool(balance_patient_period_manual_mode)
        self._card_snapshot_cache = None
        self._balance_runtime_cache = None
        try:
            card_start_dt, card_end_dt = self.service.get_day_period(date)
        except Exception:
            card_start_dt, card_end_dt = date, None
        cached_card_snapshot = self._get_cached_patient_card_snapshot(admission_id, date)
        cached_vitals_snapshot = cached_card_snapshot or self._get_cached_patient_vitals_snapshot(admission_id, date)
        return card_start_dt, card_end_dt, cached_card_snapshot, cached_vitals_snapshot

    def _sync_patient_card_layout_context(
        self,
        admission_id,
        date,
        card_start_dt,
        card_end_dt,
        orders_context_unchanged: bool,
    ):
        # Интеграция событий статуса
        self.layout_manager.current_admission_id = admission_id
        self.layout_manager.current_date = date
        self._sync_lab_orders_context()
        self._sync_plan_card_ui_state()
        self._update_emergency_notice_sector()
        self._update_chart_context_for_patient_open(admission_id, card_start_dt)

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

        self._sync_orders_widget_context_for_patient_open(admission_id, date, orders_context_unchanged)

    def _update_chart_context_for_patient_open(self, admission_id, card_start_dt):
        chart_matches_target = False
        if hasattr(self, 'chart'):
            chart_matches_target = self._chart_matches_context(admission_id, card_start_dt)
            if not chart_matches_target:
                self._last_applied_chart_signature = None
            if (
                hasattr(self.chart, "clear_for_context")
                and not chart_matches_target
            ):
                self.chart.clear_for_context(admission_id=admission_id, start_time=card_start_dt)
            else:
                self.chart.admission_id = admission_id
        elif not chart_matches_target:
            self._last_applied_chart_signature = None

    def _sync_orders_widget_context_for_patient_open(self, admission_id, date, orders_context_unchanged: bool):
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
            if not self._archive_read_only_mode and not orders_context_unchanged:
                ow.clear_drafts()

    def _schedule_patient_card_snapshots(
        self,
        admission_id,
        date,
        *,
        request_snapshot: bool,
        ensure_initial_status,
        cached_vitals_snapshot,
    ):
        if not request_snapshot:
            return
        should_ensure_initial_status = (
            self._should_ensure_initial_status_for_date(date)
            if ensure_initial_status is None
            else bool(ensure_initial_status)
        )
        if cached_vitals_snapshot:
            self._schedule_card_hydration_snapshot(
                admission_id,
                date,
                ensure_initial_status=should_ensure_initial_status,
            )
        else:
            self._request_card_snapshot(
                ensure_initial_status=should_ensure_initial_status,
                show_empty_message=False,
                load_scope="patient_open_vitals",
            )
            self._schedule_card_hydration_snapshot(
                admission_id,
                date,
                ensure_initial_status=should_ensure_initial_status,
            )

    def _activate_patient_card_vitals_tab(self):
        if hasattr(self, 'layout_manager'):
            active_tab = self.layout_manager.set_active_tab("Витальные функции", source="refresh") or "Витальные функции"
            if hasattr(self.layout_manager, 'sector_2b'):
                self.layout_manager.sector_2b.select_tab(active_tab, emit=False)
            if active_tab != "Витальные функции":
                self.on_tab_changed(active_tab)

    def _sync_patient_card_auxiliary_contexts(self, admission_id, date):
        if hasattr(self, 'balance_controller'):
            self.balance_controller.admission_id = admission_id
            self.balance_controller.shift_date = date
            if hasattr(self.balance_controller, "set_patient_period_manual_mode"):
                self.balance_controller.set_patient_period_manual_mode(self._balance_patient_period_manual_mode)

        diet_widget = self._ensure_diet_widget()
        if diet_widget:
            diet_widget.set_context(admission_id, date)

    def _schedule_nurse_orders_context_for_patient_open(self, admission_id, date, patient_open_generation: int):
        # Обновляем контекст 1а/5 явно: это нужно и при ПЕРВОМ открытии карты.
        # Важно: manager может еще не существовать, если мы пришли из списка коек.
        nurse_orders_mgr = None
        if hasattr(self.layout_manager, "ensure_nurse_orders_manager"):
            nurse_orders_mgr = self.layout_manager.ensure_nurse_orders_manager()
        elif hasattr(self.layout_manager, "nurse_orders_manager"):
            nurse_orders_mgr = self.layout_manager.nurse_orders_manager
        if nurse_orders_mgr:
            self._bind_nurse_orders_balance_signals()
            QTimer.singleShot(
                0,
                lambda mgr=nurse_orders_mgr, aid=admission_id, d=date, gen=patient_open_generation: (
                    self._set_nurse_orders_context_if_current(mgr, aid, d, gen)
                ),
            )

    def _set_nurse_orders_context_if_current(self, mgr, admission_id, date, generation: int):
        if self._is_closing or generation != self._patient_open_generation:
            return
        if int(admission_id or 0) != int(self.admission_id or 0):
            return
        if date != self._current_date:
            return
        try:
            mgr.set_context(admission_id, date)
        except RuntimeError:
            logger.debug("Doctor nurse-orders context skipped for deleted widget", exc_info=True)

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
                        bool(runtime.get("plan_card_available")),
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
        if hasattr(self, 'layout_manager'):
            self.layout_manager.current_date = self._current_date
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
        self._update_emergency_notice_sector()
        diet_widget = self._ensure_diet_widget()
        if diet_widget and self.admission_id:
            diet_widget.set_context(self.admission_id, self._current_date)
        self._sync_lab_orders_context()
        self._sync_plan_card_ui_state()
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
        from .components.control_panel import ControlPanel
        from .components.sector8_panel import Sector8Panel
        from ..shared.lightweight_w1_shell import LightweightW1Shell

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(5, 5, 5, 5)
        self.content_stack = QStackedWidget(self)

        self._w1_shell = LightweightW1Shell(
            role="doctor",
            patient_service=self.patient_service,
            remcard_service=self.service,
            parent=self.content_stack,
            operblock_service=self.operblock_service,
        )
        self.layout_manager = self._w1_shell
        self.content_stack.addWidget(self.layout_manager)
        main_layout.addWidget(self.content_stack)

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
        self.sector8_panel.electrolytes_calc_clicked.connect(self.on_electrolyte_calculator_clicked)
        self.sector8_panel.add_patient_clicked.connect(self.on_add_patient_clicked)
        self.sector8_panel.bonus_clicked.connect(self.on_bonus_clicked)
        self.sector8_panel.bars_clicked.connect(self.on_bars_clicked)
        self.sector8_panel.set_bars_auth_state(False)
        logger.info("[StartupDiag] phase=bars_auth_autocheck_disabled")

        if hasattr(self.layout_manager, 'beds_selection_widget'):
            self.layout_manager.beds_selection_widget.patient_selected.connect(self.on_patient_selected_from_list)

        if hasattr(self.layout_manager, 'sector_8'):
            self.layout_manager.sector_8.set_content(self.sector8_panel)
        if hasattr(self.layout_manager, "selection_mode_changed"):
            self.layout_manager.selection_mode_changed.connect(self._on_selection_mode_changed)
            self._on_selection_mode_changed(getattr(self.layout_manager, "current_mode", "beds"))

        # Динамические W1-экраны shell подключаются без создания полной карты.
        self._wire_dynamic_views()

    def has_full_layout(self) -> bool:
        return bool(self._full_layout_created)

    def _ensure_full_layout(self, reason: str = "") -> bool:
        if self._is_closing:
            return False
        if self._full_layout_created:
            return True
        if not hasattr(self, "content_stack"):
            return False

        from ..shared.remcard_layout import RemCardLayoutManager

        old_shell = getattr(self, "_w1_shell", None)
        layout = RemCardLayoutManager(
            role="Врач",
            patient_service=self.patient_service,
            remcard_service=self.service,
            parent=self.content_stack,
            operblock_service=self.operblock_service,
        )
        layout.patient_status_service = self.service.status_service
        layout.current_admission_id = self.admission_id
        layout.current_date = self._current_date
        layout.remcard_service = self.service
        layout.operblock_service = self.operblock_service

        self.content_stack.addWidget(layout)
        self.layout_manager = layout
        self._full_layout_created = True
        self._full_layout_static_signals_bound = False
        self._wire_full_layout_signals()
        self.content_stack.setCurrentWidget(layout)
        self._retire_w1_shell(old_shell)
        logger.info("[DOCTOR_VIEW] lazy full layout created reason=%s", reason)
        return True

    def _wire_full_layout_signals(self):
        if self._full_layout_static_signals_bound:
            return
        layout = getattr(self, "layout_manager", None)
        if layout is None or layout is getattr(self, "_w1_shell", None):
            return

        if hasattr(layout, 'orders_widget'):
            layout.orders_widget.service = self.service

        if hasattr(layout, 'beds_selection_widget'):
            layout.beds_selection_widget.patient_selected.connect(self.on_patient_selected_from_list)

        if hasattr(layout, 'sector_8'):
            layout.sector_8.set_content(self.sector8_panel)
        if hasattr(layout, "selection_mode_changed"):
            layout.selection_mode_changed.connect(self._on_selection_mode_changed)
            self._on_selection_mode_changed(getattr(layout, "current_mode", "beds"))

        self._bind_orders_widget_signals()

        if (
            hasattr(layout, 'sector_7na_b')
            and hasattr(layout.sector_7na_b, 'data_layout')
        ):
            layout.sector_7na_b.data_layout.addWidget(self.controls)

        if hasattr(layout, 'sector_7anal_b') and hasattr(layout.sector_7anal_b, 'yesterday_labs_requested'):
            layout.sector_7anal_b.yesterday_labs_requested.connect(self.on_yesterday_lab_orders_clicked)

        if hasattr(layout, 'sector_2b'):
            layout.sector_2b.tab_changed.connect(self.on_tab_changed)

        if hasattr(layout, "register_events_status_handler"):
            layout.register_events_status_handler(self.force_reload_all)
        elif hasattr(layout, 'sector_events') and layout.sector_events:
            layout.sector_events.status_changed.connect(self.force_reload_all)

        if hasattr(layout, 'sector_3b'):
            layout.sector_3b.out_values_changed.connect(self.on_out_values_changed)

        if hasattr(layout, 'sector_4v'):
            s4v = layout.sector_4v
            s4v.archive_requested.connect(self.show_archive)
            s4v.show_card_requested.connect(self.on_show_card_clicked)
            s4v.create_card_requested.connect(self.on_create_card_clicked)
            s4v.plan_card_requested.connect(self.on_plan_card_clicked)
            s4v.yest_card_requested.connect(self.on_yest_card_clicked)
            s4v.full_report_requested.connect(self.on_full_report_clicked)
            s4v.daily_report_requested.connect(self.on_daily_report_clicked)

        self._archive_signals_bound = False
        self._admin_signals_bound = False
        self._wire_dynamic_views()
        self._full_layout_static_signals_bound = True

    def _retire_w1_shell(self, shell):
        if shell is None:
            return
        try:
            if hasattr(shell, "beds_selection_widget"):
                shell.beds_selection_widget.patient_selected.disconnect(self.on_patient_selected_from_list)
        except Exception:
            pass
        try:
            if hasattr(shell, "selection_mode_changed"):
                shell.selection_mode_changed.disconnect(self._on_selection_mode_changed)
        except Exception:
            pass
        try:
            if hasattr(shell, "shutdown"):
                shell.shutdown()
        except Exception:
            logger.debug("Doctor W1 shell shutdown failed", exc_info=True)
        try:
            if self.content_stack.indexOf(shell) >= 0:
                self.content_stack.removeWidget(shell)
        except Exception:
            pass
        shell.deleteLater()
        self._w1_shell = None

    def _wire_dynamic_views(self):
        archive_widget = getattr(self.layout_manager, "archive_widget", None)
        if archive_widget and not self._archive_signals_bound:
            archive_widget.back_requested.connect(lambda: self.on_back_clicked())
            archive_widget.patient_selected.connect(self.on_patient_selected_from_archive)
            archive_widget.edit_requested.connect(self.on_patient_edit_requested_from_archive)
            if hasattr(archive_widget, "operblock_case_selected"):
                archive_widget.operblock_case_selected.connect(self.on_operblock_case_selected_from_archive)
            self._archive_signals_bound = True

        admin_widget = getattr(self.layout_manager, "admin_widget", None)
        if admin_widget and not self._admin_signals_bound:
            admin_widget.btn_back_to_roles.clicked.connect(lambda: self.on_back_clicked())
            self._admin_signals_bound = True

    def _close_operblock_archive_viewer(self):
        viewer = getattr(self, "_operblock_archive_viewer", None)
        if self._is_qobject_alive(viewer):
            try:
                if hasattr(viewer, "shutdown"):
                    viewer.shutdown()
            except Exception as exc:
                logger.warning("Failed to shutdown operblock archive viewer: %s", exc)
            try:
                if hasattr(self, "content_stack") and self.content_stack.indexOf(viewer) >= 0:
                    self.content_stack.removeWidget(viewer)
            except Exception:
                pass
            try:
                viewer.deleteLater()
            except Exception:
                pass
        self._operblock_archive_viewer = None
        if self._operblock_archive_db_manager is not None:
            try:
                self._operblock_archive_db_manager.close()
            except Exception as exc:
                logger.warning("Failed to close operblock archive read-only DB manager: %s", exc)
        self._operblock_archive_db_manager = None
        self._operblock_archive_source_db_path = None

    def _ensure_operblock_archive_viewer(self, source_db_path: str | None = None):
        source_db = os.path.abspath(str(source_db_path or "")) if source_db_path else None
        viewer = getattr(self, "_operblock_archive_viewer", None)
        current_source = getattr(self, "_operblock_archive_source_db_path", None)
        if self._is_qobject_alive(viewer) and (source_db or None) == (current_source or None):
            return viewer
        if self._is_qobject_alive(viewer):
            self._close_operblock_archive_viewer()
        if self.operblock_service is None:
            CustomMessageBox.warning(self, "Архив оперблока", "Сервис оперблока недоступен.")
            return None
        if not hasattr(self, "content_stack"):
            CustomMessageBox.warning(self, "Архив оперблока", "Не удалось открыть просмотр протокола.")
            return None

        from rem_card.ui.operblock_view.operblock_main_widget import OperBlockMainWidget

        patient_service = self.patient_service
        remcard_service = self.service
        operblock_service = self.operblock_service
        db_manager = None
        if source_db:
            try:
                remcard_service, db_manager = create_archive_readonly_service(source_db)
                from rem_card.services.operblock_service import OperBlockService

                operblock_service = OperBlockService(db_manager)
                patient_service = getattr(remcard_service, "_patients", patient_service)
            except Exception as exc:
                CustomMessageBox.warning(self, "Архив оперблока", f"Не удалось открыть архивную БД:\n{exc}")
                return None

        try:
            viewer = OperBlockMainWidget(
                patient_service,
                remcard_service,
                operblock_service,
                parent=self.content_stack,
                view_only=True,
            )
        except Exception:
            if db_manager is not None:
                try:
                    db_manager.close()
                except Exception:
                    pass
            raise
        viewer.view_back_requested.connect(self._return_from_operblock_archive_viewer)
        self._operblock_archive_viewer = viewer
        self._operblock_archive_db_manager = db_manager
        self._operblock_archive_source_db_path = source_db
        self.content_stack.addWidget(viewer)
        return viewer

    def on_operblock_case_selected_from_archive(self, case):
        try:
            case_id = int((case or {}).get("source_operation_case_id") or (case or {}).get("operation_case_id") or 0)
        except Exception:
            case_id = 0
        if not case_id:
            CustomMessageBox.warning(self, "Архив оперблока", "Не удалось определить запись оперблока.")
            return

        source_db_path = str((case or {}).get("source_db_path") or "").strip() if (case or {}).get("is_external_archive") else ""
        viewer = self._ensure_operblock_archive_viewer(source_db_path or None)
        if viewer is None:
            return
        self._card_return_mode = None
        self._card_opened_from_global_archive = False
        self._exit_archive_read_only_mode()
        self.content_stack.setCurrentWidget(viewer)
        viewer.open_archive_protocol(case_id)

    def _return_from_operblock_archive_viewer(self):
        if hasattr(self, "content_stack") and hasattr(self, "layout_manager"):
            self.content_stack.setCurrentWidget(self.layout_manager)
        if getattr(self, "_operblock_archive_source_db_path", None):
            self._close_operblock_archive_viewer()
        if hasattr(self, "layout_manager"):
            self.layout_manager.set_patient_selection_mode("archive")
            self._wire_dynamic_views()
            if hasattr(self.layout_manager, "_refresh_archive_if_needed"):
                self.layout_manager._refresh_archive_if_needed(force=True)
            if hasattr(self.layout_manager, "bottom_row"):
                self.layout_manager.bottom_row.hide()

    def _ensure_orders_widget(self):
        layout = getattr(self, "layout_manager", None)
        if layout is None:
            return None
        if hasattr(layout, "ensure_orders_widget"):
            ow = layout.ensure_orders_widget()
        else:
            ow = getattr(layout, "orders_widget", None)
        if ow is not None:
            ow.service = self.service
            self._bind_orders_widget_signals(ow)
        return ow

    def _bind_orders_widget_signals(self, ow=None):
        if self._orders_widget_signals_bound:
            return
        ow = ow or getattr(getattr(self, "layout_manager", None), "orders_widget", None)
        if ow is None or not hasattr(self, "controls"):
            return
        ow.draftStatusChanged.connect(self.controls.set_save_active)
        ow.draftStatusChanged.connect(self.controls.set_rollback_active)
        ow.administrationStatusChanged.connect(self.controls.set_clean_active)
        ow.ordersPresenceChanged.connect(self.controls.set_clear_active)
        ow.draftStatusChanged.connect(self._schedule_balance_update)
        ow.administrationStatusChanged.connect(self._schedule_balance_update)
        ow.ordersPresenceChanged.connect(self._schedule_balance_update)
        if hasattr(ow, "localBalanceChanged"):
            ow.localBalanceChanged.connect(self._schedule_balance_update)
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
        self._orders_widget_signals_bound = True

    def _on_selection_mode_changed(self, mode: str):
        if (
            str(mode or "") == "beds"
            and self._full_layout_created
            and getattr(getattr(self, "layout_manager", None), "current_mode", None) == "card"
        ):
            logger.debug("Doctor ignored stale beds selection signal during card mode")
            return
        self._selection_mode = str(mode or "")
        if self._selection_mode != PATIENT_BED_MANAGEMENT_MODE:
            self._release_add_patient_lock()
        self._refresh_add_patient_button_lock_state()

    def _schedule_card_ui_prewarm(self):
        if not self._full_layout_created:
            return
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
            ow = self._ensure_orders_widget()
            if ow is not None and getattr(ow, "main_layout", None) is None:
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
                self._bind_nurse_orders_balance_signals()
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
            if not JOURNAL_WIDGET_PREWARM_ENABLED:
                self._journal_prewarm_done = True
                return

            if hasattr(self, "layout_manager") and hasattr(self.layout_manager, "prewarm_journal_widget"):
                self.layout_manager.prewarm_journal_widget()
                self._journal_prewarm_done = True
                logger.debug("Doctor patient-bed management widget prewarm completed")
        except Exception as exc:
            logger.warning("Doctor patient-bed management prewarm failed: %s", exc)
        finally:
            if not self._journal_prewarm_done:
                self._journal_prewarm_started = False

    def _schedule_chart_init(self, delay_ms: int = CHART_LAZY_INIT_DELAY_MS):
        if getattr(self, "chart", None) is not None or self._chart_init_pending:
            return
        self._chart_init_pending = True
        QTimer.singleShot(max(0, int(delay_ms or 0)), self._run_deferred_chart_init)

    def _run_deferred_chart_init(self):
        self._chart_init_pending = False
        if self._is_closing:
            return
        try:
            self._ensure_chart_initialized()
        except Exception as exc:
            logger.warning("Doctor chart lazy init failed: %s", exc, exc_info=True)

    def _ensure_chart_initialized(self) -> bool:
        if getattr(self, "chart", None) is not None:
            return True
        if not hasattr(self, "layout_manager") or not getattr(self.layout_manager, "sector_2v", None):
            return False

        from ..shared.chart_widget import ChartWidget

        self.chart = ChartWidget()
        self.chart.service = self.service
        self.chart.status_service = self.service.status_service
        self.chart.admission_id = self.admission_id
        self.layout_manager.sector_2v.set_content(self.chart)
        self._update_chart_from_snapshot(self._card_snapshot_cache or {})
        return True

    def _update_chart_from_snapshot(self, snapshot: dict) -> None:
        if not snapshot or getattr(self, "chart", None) is None:
            return
        runtime = snapshot.get("balance_runtime") or {}
        chart_signature = self._chart_snapshot_signature(snapshot)
        if (
            chart_signature is not None
            and chart_signature == self._last_applied_chart_signature
        ):
            logger.info(
                "DoctorRemCardWidget skipped unchanged chart snapshot admission_id=%s scope=%s version=%s",
                snapshot.get("admission_id"),
                snapshot.get("scope"),
                snapshot.get("version"),
            )
            return
        self.chart.update_data(
            snapshot.get("vitals_extended") or [],
            snapshot.get("start_dt"),
            active_intervals=snapshot.get("chart_active_intervals") or runtime.get("active_intervals"),
        )
        self._last_applied_chart_signature = chart_signature

    def _ensure_card_widgets_initialized(self):
        if self._card_widgets_initialized:
            return
        if not self._full_layout_created:
            return

        from rem_card.ui.shared.vitals_widget import VitalsWidget
        from .components.balance_controller import BalanceController

        self.vitals_input = VitalsWidget(
            self.service,
            self.admission_id,
            self._current_date,
            allow_future_input=True,
        )
        self.vitals_input.save_btn.clicked.connect(self.refresh_data)
        self.vitals_input.data_changed.connect(self.refresh_data)
        self.layout_manager.sector_1b.set_content(self.vitals_input)

        self.balance_controller = BalanceController(self.service.fluid_service, self.admission_id, self._current_date)
        self._bind_balance_widgets_if_ready()

        self._card_widgets_initialized = True
        self._schedule_chart_init()

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
            if hasattr(self.balance_controller, "set_patient_period_manual_mode"):
                self.balance_controller.set_patient_period_manual_mode(self._balance_patient_period_manual_mode)
        if not self._apply_balance_snapshot_if_available():
            self._request_card_snapshot(load_scope="full")

    def on_show_card_clicked(self):
        if self._is_plan_card_open():
            now = datetime.now()
            if self.service.has_card(self.admission_id, now):
                self.safe_load_archived_card(now)
            else:
                CustomMessageBox.information(self, "Пусто", "Карта за текущие сутки еще не создана.")
            return

        now = datetime.now()
        start, end = self.service.get_day_period(now)
        if not (start <= self._current_date < end):
            if self._card_opened_from_global_archive:
                latest_date = self._latest_created_card_date(self.admission_id)
                if not latest_date:
                    CustomMessageBox.information(self, "Пусто", "У пациента нет сохраненных карт.")
                    return
                if self._is_same_medical_day(latest_date, self._current_date):
                    self.refresh_data(show_empty_message=True)
                    return
                self.safe_load_archived_card(
                    latest_date,
                    balance_patient_period_manual_mode=True,
                )
                return
            if self.service.has_card(self.admission_id, now):
                self.safe_load_archived_card(now)
            else:
                CustomMessageBox.information(self, "Пусто", "Карта за текущие сутки еще не создана.")
        else:
            self.refresh_data(show_empty_message=True)

    def _admission_status_is_outcome(self, admission_id: int) -> bool:
        if self.admission_id and int(self.admission_id) == int(admission_id) and self._current_status_is_outcome():
            return True
        if not self.service or not hasattr(self.service, "get_current_status"):
            return False
        try:
            status_dto = self.service.get_current_status(int(admission_id))
        except Exception:
            status_dto = None
        status_value = getattr(status_dto, "status", None)
        return bool(status_dto and getattr(status_value, "is_outcome", lambda: False)())

    def on_plan_card_clicked(self):
        if not self.admission_id:
            return
        self._open_or_create_plan_card(int(self.admission_id))

    def _open_or_create_plan_card(self, admission_id: int, patient=None) -> bool:
        if self._archive_read_only_mode:
            self._show_read_only_hint()
            return False
        if self._admission_status_is_outcome(admission_id):
            CustomMessageBox.information(
                self,
                "Плановая карта",
                "Плановая карта недоступна, пока у пациента не отменен исход.",
            )
            return False

        state = self._plan_card_state_for_admission(admission_id)
        if not state.get("plan_card_available"):
            CustomMessageBox.information(
                self,
                "Плановая карта",
                "Плановая карта доступна только при созданной текущей карте в последний час смены.",
            )
            return False

        target_date = state.get("plan_card_target_date")
        if target_date is None:
            target_date = self.service.get_day_period(datetime.now())[1]

        plan_exists = bool(state.get("plan_card_exists"))
        if not plan_exists:
            try:
                plan_exists = bool(self.service.has_card(admission_id, target_date))
            except Exception:
                plan_exists = False

        self.load_patient_card(
            admission_id,
            target_date,
            request_snapshot=plan_exists,
            ensure_initial_status=False,
        )
        if patient is not None:
            self._prime_patient_header_from_w1(patient, target_date)
        if hasattr(self, "layout_manager") and hasattr(self.layout_manager, "set_patient_selection_mode"):
            self.layout_manager.set_patient_selection_mode("card")

        if not plan_exists:
            self.on_create_card_clicked(target_date=target_date, planned=True)
        return True

    def on_create_card_clicked(self, target_date=None, planned: bool = False):
        if self._archive_read_only_mode:
            self._show_read_only_hint()
            return
        if self._current_status_is_outcome():
            CustomMessageBox.information(
                self,
                "Создание карты",
                "Создание новой карты недоступно, пока у пациента не отменен исход.",
            )
            self._apply_archive_read_only_state()
            return
        if self._create_card_write_pending:
            return
        if self._snapshot_worker is not None:
            if not self._create_card_after_snapshot:
                logger.info(
                    "DoctorRemCardWidget defers create-card write until snapshot load finishes admission_id=%s",
                    self.admission_id,
                )
            self._create_card_after_snapshot = {
                "target_date": target_date,
                "planned": bool(planned),
            }
            self._snapshot_pending = None
            return

        target_date = target_date or datetime.now()
        start, _ = self.service.get_day_period(target_date)
        patient = self.service.get_patient(self.admission_id)
        adm_dt = patient.admission_datetime if patient else None
        vital_time = start
        if patient and patient.admission_datetime and start < patient.admission_datetime:
            vital_time = patient.admission_datetime
            
        from rem_card.data.dto.remcard_dto import VitalDTO
        dto = VitalDTO(id=None, admission_id=self.admission_id, timestamp=vital_time,
                       sys=None, dia=None, pulse=None, temp=None, spo2=None, rr=None, cvp=None)
        admission_id = self.admission_id
        service = self.service
        ensure_guard = getattr(self, "_should_ensure_initial_status_for_date", None)
        should_ensure_initial_status = bool(ensure_guard(target_date)) if callable(ensure_guard) else True

        def operation():
            if admission_id and service.status_service and should_ensure_initial_status:
                service.status_service.ensure_initial_status(admission_id, start, adm_dt)
            service.add_vital(dto, shift_date=target_date, force=True)
            return True

        def on_success(_result):
            self._finish_create_card_pending()
            if self.admission_id != admission_id:
                return
            if self.service.status_service:
                self.layout_manager.refresh_current_status()
            if hasattr(self, 'vitals_input'):
                self.vitals_input.update_undo_button_state()
                self.vitals_input.data_changed.emit()
            self.update_patient_info()
            message = (
                "Плановая карта успешно создана. Вы можете заполнить её заранее."
                if planned
                else "Карта успешно создана. Вы можете приступить к её заполнению."
            )
            CustomMessageBox.information(self, "Создание карты", message)

        def on_error(exc):
            self._finish_create_card_pending()
            logger.error(f"Error creating empty vital for card: {exc}", exc_info=(type(exc), exc, exc.__traceback__))
            try:
                self.force_reload_all()
            except Exception:
                logger.warning("Failed to refresh after create-card error", exc_info=True)
            CustomMessageBox.warning(self, "Создание карты", f"Не удалось создать карту: {exc}")

        self._begin_create_card_pending()
        try:
            if hasattr(service, "enqueue_write"):
                service.enqueue_write(
                    f"doctor_create_empty_card:{'plan:' if planned else ''}{admission_id}",
                    operation,
                    on_success=on_success,
                    on_error=on_error,
                )
                return
            result = operation()
        except Exception as exc:
            on_error(exc)
            return
        on_success(result)

    def _begin_create_card_pending(self):
        self._create_card_write_pending = True
        self._set_create_card_controls_enabled(False)

    def _finish_create_card_pending(self):
        self._create_card_write_pending = False
        self._set_create_card_controls_enabled(True)

    def _set_create_card_controls_enabled(self, enabled: bool):
        sector = getattr(getattr(self, "layout_manager", None), "sector_4v", None)
        snapshot = getattr(self, "_card_snapshot_cache", None) or {}
        state_builder = getattr(self, "_sector_4v_button_state", None)
        if callable(state_builder):
            card_exists, _yest_exists, plan_card_available = state_builder(snapshot)
        else:
            card_exists = bool(snapshot.get("card_exists"))
            plan_card_available = bool(snapshot.get("plan_card_available"))
        outcome_checker = getattr(self, "_current_status_is_outcome_safe", None)
        if callable(outcome_checker):
            is_outcome = bool(outcome_checker())
        else:
            legacy_checker = getattr(self, "_current_status_is_outcome", None)
            is_outcome = bool(legacy_checker()) if callable(legacy_checker) else False
        can_edit = bool(enabled) and not is_outcome
        button = getattr(sector, "btn_new_card", None)
        if button is not None:
            button.setEnabled(can_edit and not card_exists)
        plan_button = getattr(sector, "btn_plan_card", None)
        if plan_button is not None:
            plan_button.setEnabled(can_edit and plan_card_available)

    def on_yest_card_clicked(self):
        reference_date = self._card_button_reference_date()
        try:
            reference_start, _reference_end = self.service.get_day_period(reference_date)
            yest = reference_start - timedelta(days=1)
        except Exception:
            yest = reference_date - timedelta(days=1)
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
                        QTimer.singleShot(
                            100,
                            lambda: self.safe_load_archived_card(
                                target_dt,
                                patient.id,
                                balance_patient_period_manual_mode=True,
                            ),
                        )
                    else:
                        self._balance_patient_period_manual_mode = True
                        if hasattr(self, "balance_controller") and hasattr(
                            self.balance_controller,
                            "set_patient_period_manual_mode",
                        ):
                            self.balance_controller.set_patient_period_manual_mode(True)
        except Exception as e:
            logger.error(f"Error showing archive: {e}", exc_info=True)

    def safe_load_archived_card(
        self,
        selected_date,
        admission_id=None,
        *,
        balance_patient_period_manual_mode: bool = False,
    ):
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
            
            should_ensure_initial_status = self._should_ensure_initial_status_for_date(selected_date)
            if target_id and self.service.status_service and should_ensure_initial_status:
                s_start, _ = self.service.get_day_period(selected_date)
                patient = self.service.get_patient(target_id)
                adm_dt = patient.admission_datetime if patient else None
                self.service.status_service.ensure_initial_status(target_id, s_start, adm_dt)
            elif target_id and self.service.status_service and not self._archive_read_only_mode:
                logger.info(
                    "[ARCHIVE] skip initial status write for historical card admission_id=%s date=%s",
                    target_id,
                    selected_date.isoformat() if hasattr(selected_date, "isoformat") else selected_date,
                )

            if admission_id is not None:
                self.admission_id = admission_id
                self.layout_manager.current_admission_id = admission_id
                self.layout_manager.current_date = selected_date
                self._sync_lab_orders_context()
                self.layout_manager.set_patient_selection_mode("card")
                self.layout_manager.sync_bottom_row_visibility_to_current_tab()

            self.current_date = selected_date
            self._balance_patient_period_manual_mode = bool(balance_patient_period_manual_mode)
            if hasattr(self.layout_manager, 'nurse_orders_manager') and self.layout_manager.nurse_orders_manager:
                self._bind_nurse_orders_balance_signals()
                self.layout_manager.nurse_orders_manager.set_context(target_id, self._current_date)
            self.force_reload_all(ensure_initial_status=should_ensure_initial_status)
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

    def force_reload_all(self, *_, ensure_initial_status=None):
        self._ensure_card_widgets_initialized()
        from ...app.logger import logger
        logger.debug("[RELOAD] --- Beginning full reload sequence ---")
        try:
            should_ensure_initial_status = (
                self._should_ensure_initial_status_for_date(self._current_date)
                if ensure_initial_status is None
                else bool(ensure_initial_status)
            )
            if hasattr(self, 'balance_controller'):
                self.balance_controller.shift_date = self._current_date
                if hasattr(self.balance_controller, "set_patient_period_manual_mode"):
                    self.balance_controller.set_patient_period_manual_mode(self._balance_patient_period_manual_mode)
                
            self._request_card_snapshot(
                ensure_initial_status=should_ensure_initial_status,
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
        if not self.service or self._current_date is None:
            return
        now = datetime.now()
        current_start, current_end = self.service.get_day_period(now)
        reference_date = self._card_button_reference_date()
        is_today = current_start <= reference_date < current_end
        active = bool(is_today and not self._archive_read_only_mode)
        if hasattr(self, 'controls'):
            self.controls.set_yesterday_active(active)
        self._set_lab_yesterday_button_active(active)

    def _set_lab_yesterday_button_active(self, active: bool):
        sector_7anal_b = getattr(getattr(self, "layout_manager", None), "sector_7anal_b", None)
        if sector_7anal_b is not None and hasattr(sector_7anal_b, "set_yesterday_active"):
            sector_7anal_b.set_yesterday_active(bool(active))

    def on_yesterday_lab_orders_clicked(self):
        if self._archive_read_only_mode:
            self._show_read_only_hint()
            return
        layout = getattr(self, "layout_manager", None)
        if layout is None:
            return
        if hasattr(layout, "_ensure_anal_tab_initialized"):
            layout._ensure_anal_tab_initialized()
        sector_anal = getattr(layout, "sector_anal", None)
        if sector_anal is None or not hasattr(sector_anal, "load_yesterday_lab_orders"):
            CustomMessageBox.warning(self, "Анализы", "Сектор анализов сейчас недоступен.")
            return
        if hasattr(sector_anal, "set_context"):
            sector_anal.set_context(self.service, self.admission_id, self._current_date)
        sector_anal.load_yesterday_lab_orders()

    def on_out_values_changed(self, new_total_out):
        self.update_balance_data()

    def update_balance_data(self):
        self._ensure_card_widgets_initialized()
        if self._sync_plan_card_ui_state():
            self._apply_archive_read_only_state()
            self._update_yesterday_button_state()
        self._bind_balance_widgets_if_ready()
        if self.admission_id is None: return
        if self._balance_calculator_cls is None:
            from ...services.balance_calculator import BalanceCalculator

            self._balance_calculator_cls = BalanceCalculator
        runtime = self._balance_runtime_cache or {}
        if not runtime:
            return
        local_orders = build_balance_orders_from_orders_widget(
            getattr(self.layout_manager, "orders_widget", None),
            self.admission_id,
            self._current_date,
            tab_active=self._is_orders_tab_active(),
        )
        orders = local_orders if local_orders is not None else (runtime.get("orders") or [])
        panel_orders = apply_current_order_mark_overrides(
            orders,
            getattr(self.layout_manager, "nurse_orders_manager", None),
            self.admission_id,
            self._current_date,
        )
        if panel_orders is not None:
            orders = panel_orders

        now = datetime.now()
        start = runtime.get("start_dt")
        end = runtime.get("end_dt")
        calc_time = now if start and end and start <= now < end else end
        calc_res = self._balance_calculator_cls.calculate(
            orders=orders,
            current_time=calc_time,
            end_of_card=end,
            transfer_time=runtime.get("transfer_time"),
            active_intervals=runtime.get("active_intervals") or [],
            outcome_time=runtime.get("outcome_time"),
        )
        
        cur, day = calc_res["current"], calc_res["daily"]
        local_oral_state = self._local_oral_state_for_balance()
        oral_kwargs = {}
        if local_oral_state is not None:
            oral_kwargs["oral_events"] = local_oral_state[0]
            oral_kwargs["oral_plan"] = local_oral_state[1]
        oral_cur, oral_day = oral_totals_from_runtime(runtime, calc_time, **oral_kwargs)
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
        if hasattr(self.layout_manager, "sector_2b") and hasattr(self.layout_manager.sector_2b, "current_tab_name"):
            tab_name = self.layout_manager.sector_2b.current_tab_name() or tab_name
        if tab_name == "Баланс жидкости":
            self._ensure_balance_tab_ready()
        elif tab_name == "Назначения":
            show_started = time.perf_counter()
            admission_id = self.admission_id
            mark_foreground_activity(
                "orders_show",
                admission_id=admission_id,
                source="click",
                ttl_sec=CARD_HYDRATION_FOREGROUND_IDLE_SEC,
            )
            record_metric(
                "orders_show_start",
                1,
                admission_id=admission_id,
                source="click",
            )
            logger.info(
                "[OrdersShow] orders_show_start admission_id=%s source=click",
                admission_id,
            )
            show_source = "click"
            show_status = "started"
            is_draft = None
            try:
                ow = self._ensure_orders_widget()
                if ow is None:
                    logger.warning("Doctor orders tab requested, but orders widget was not initialized")
                    show_status = "widget_missing"
                    return
                self._bind_orders_widget_signals(ow)
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
                had_ready_model = bool(
                    getattr(ow, "model", None) is not None
                    and getattr(ow.model, "admission_id", None) == ow.admission_id
                    and getattr(ow.model, "shift_date", None) == ow.shift_date
                    and not getattr(ow, "_snapshot_stale", False)
                )
                ow.ensure_ready_for_show()
                show_source = "cache" if had_ready_model and getattr(ow, "_snapshot_worker", None) is None else "refresh"

                is_draft = ow.has_drafts()

                # Проверяем статусы кнопок управления
                self.controls.set_save_active(is_draft)
                self.controls.set_rollback_active(is_draft)
                self.controls.set_clean_active(ow.has_administrations())
                self.controls.set_clear_active(ow.has_orders())
                show_status = "ok"
            except Exception:
                show_status = "error"
                raise
            finally:
                elapsed_ms = (time.perf_counter() - show_started) * 1000.0
                record_metric(
                    "orders_show_end",
                    round(elapsed_ms, 3),
                    admission_id=admission_id,
                    source=show_source,
                    status=show_status,
                    has_drafts=None if is_draft is None else int(bool(is_draft)),
                )
                logger.info(
                    "[OrdersShow] orders_show_end admission_id=%s source=%s status=%s elapsed_ms=%.2f has_drafts=%s",
                    admission_id,
                    show_source,
                    show_status,
                    elapsed_ms,
                    None if is_draft is None else int(bool(is_draft)),
                )
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
        viewer = getattr(self, "_operblock_archive_viewer", None)
        if (
            hasattr(self, "content_stack")
            and self._is_qobject_alive(viewer)
            and self.content_stack.currentWidget() == viewer
        ):
            self._return_from_operblock_archive_viewer()
            return
        self._balance_update_timer.stop()
        current_idx = self.layout_manager.selection_stack.currentIndex()
        journal_idx = -1
        if hasattr(self.layout_manager, "journal_view"):
            journal_idx = self.layout_manager.selection_stack.indexOf(self.layout_manager.journal_view)
        was_journal_mode = (current_idx == journal_idx and journal_idx != -1)
        admin_idx = -1
        if hasattr(self.layout_manager, "admin_view"):
            admin_idx = self.layout_manager.selection_stack.indexOf(self.layout_manager.admin_view)

        if current_idx == admin_idx and admin_idx != -1:
            admin_widget = getattr(self.layout_manager, "admin_widget", None)
            if admin_widget is not None and hasattr(admin_widget, "go_back") and admin_widget.go_back():
                return
            if self._return_from_settings():
                return

        if current_idx == 0 and self._card_return_mode == "archive":
            if hasattr(self.layout_manager, 'orders_widget') and not self._archive_read_only_mode:
                self.layout_manager.orders_widget.clear_drafts()
            self.admission_id = None
            self._release_add_patient_lock()
            self._exit_archive_read_only_mode()
            self._card_return_mode = None
            self._card_opened_from_global_archive = False
            self.layout_manager.set_patient_selection_mode("archive")
            self._wire_dynamic_views()
            self.layout_manager.bottom_row.hide()
        elif current_idx == 0:
            if hasattr(self.layout_manager, 'orders_widget') and not self._archive_read_only_mode:
                self.layout_manager.orders_widget.clear_drafts()
            self.admission_id = None
            self._release_add_patient_lock()
            self._exit_archive_read_only_mode()
            self._card_return_mode = None
            self._card_opened_from_global_archive = False
            self.layout_manager.set_patient_selection_mode("beds")
            self.layout_manager.bottom_row.show()
            if was_journal_mode:
                self._force_beds_refresh_after_journal_exit()
        elif current_idx in (2, 3, 4):
            # Явно снимаем lock перед выходом из журнала/режимов выбора.
            self._release_add_patient_lock()
            self._exit_archive_read_only_mode()
            self._card_return_mode = None
            self._card_opened_from_global_archive = False
            self.layout_manager.set_patient_selection_mode("beds")
            self.layout_manager.bottom_row.show()
            if was_journal_mode:
                self._force_beds_refresh_after_journal_exit()
        else:
            self._release_add_patient_lock()
            self._exit_archive_read_only_mode()
            self._card_return_mode = None
            self._card_opened_from_global_archive = False
            self.back_to_roles_requested.emit()

    def on_settings_clicked(self):
        self._remember_settings_return_mode()
        self._exit_archive_read_only_mode()
        self.layout_manager.set_patient_selection_mode("admin")
        self._wire_dynamic_views()
        self.layout_manager.bottom_row.hide()
        admin_widget = getattr(self.layout_manager, "admin_widget", None)
        if admin_widget:
            admin_widget.set_print_context(self.service, self.admission_id, self._current_date)

    def _remember_settings_return_mode(self):
        mode = self._resolve_selection_mode()
        if mode and mode != "admin":
            self._settings_return_mode = mode

    def _return_from_settings(self) -> bool:
        mode = str(self._settings_return_mode or "").strip()
        self._settings_return_mode = None
        if not mode or mode == "admin":
            return False

        self._release_add_patient_lock()
        if mode == "archive":
            self.layout_manager.set_patient_selection_mode("archive")
            self._wire_dynamic_views()
            self.layout_manager.bottom_row.hide()
            return True
        if mode in (PATIENT_BED_MANAGEMENT_MODE, "journal"):
            self.layout_manager.set_patient_selection_mode(PATIENT_BED_MANAGEMENT_MODE)
            self.layout_manager.bottom_row.hide()
            return True
        if mode == "card" and self.admission_id is not None:
            self.layout_manager.set_patient_selection_mode("card")
            return True

        self.layout_manager.set_patient_selection_mode("beds")
        self.layout_manager.bottom_row.show()
        return True

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
            self.layout_manager.set_patient_selection_mode(PATIENT_BED_MANAGEMENT_MODE)
            self.layout_manager.bottom_row.hide()
        except Exception:
            self._release_add_patient_lock()
            raise

    def _get_bars_auth_service(self):
        if self._bars_auth_service is None:
            from rem_card.services.bars_auth_service import BarsAuthService

            self._bars_auth_service = BarsAuthService()
        return self._bars_auth_service

    def _set_bars_auth_state(self, authorized: bool):
        panel = getattr(self, "sector8_panel", None)
        if self._is_qobject_alive(panel) and hasattr(panel, "set_bars_auth_state"):
            panel.set_bars_auth_state(bool(authorized))

    def on_bars_clicked(self):
        from rem_card.ui.doctor_view.bars_auth_dialog import BarsAuthDialog

        service = self._get_bars_auth_service()
        dialog = BarsAuthDialog(service, self)
        try:
            dialog.exec()
        finally:
            deactivate = getattr(service, "deactivate", None)
            if callable(deactivate):
                deactivate()
            self._bars_auth_service = None
            self._set_bars_auth_state(False)

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
                    ensure_initial_status=self._should_ensure_initial_status_for_date(self._current_date),
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

    def on_electrolyte_calculator_clicked(self):
        from .components.electrolyte_calculator import ElectrolyteCalculatorDialog

        dialog = ElectrolyteCalculatorDialog(
            parent=self,
            patient_context=self._build_electrolyte_calculator_context(),
        )
        dialog.exec()

    def _build_electrolyte_calculator_context(self) -> dict:
        admission_id = getattr(self, "admission_id", None)
        if not admission_id:
            return {}

        snapshot = self._card_snapshot_cache or {}
        patient = snapshot.get("patient")
        if patient is None and hasattr(self.service, "get_patient"):
            try:
                patient = self.service.get_patient(int(admission_id))
            except Exception as exc:
                logger.warning("Electrolyte calculator: failed to load patient context: %s", exc)
                patient = None

        context: dict = {}
        age_years = self._electrolyte_context_age_years(patient)
        if age_years is not None:
            context["age_years"] = age_years

        sex = self._electrolyte_context_sex(patient)
        if sex:
            context["sex"] = sex

        weight_kg = self._electrolyte_context_weight_kg(int(admission_id))
        if weight_kg is not None:
            context["weight_kg"] = weight_kg

        urine_ml_day = self._electrolyte_context_last_24h_diuresis(patient, int(admission_id))
        if urine_ml_day is not None:
            context["urine_ml_day"] = urine_ml_day
        return context

    def _electrolyte_context_age_years(self, patient) -> int | None:
        if patient is None:
            return None
        try:
            from rem_card.app.patient_age import calculate_age_components

            components = calculate_age_components(getattr(patient, "birth_date", None), datetime.now())
            if components is not None:
                return int(components.years)
        except Exception:
            pass
        unit = str(getattr(patient, "age_unit", "") or "").lower()
        age = getattr(patient, "age", None)
        if age in (None, "") or "меся" in unit:
            return None
        try:
            return int(age)
        except Exception:
            return None

    def _electrolyte_context_sex(self, patient) -> str | None:
        if patient is None:
            return None
        value = str(self._electrolyte_patient_value(patient, "patient_gender") or "").strip().casefold()
        if not value:
            return None
        if value.startswith("жен") or value in {"ж", "female", "woman", "f"}:
            return "female"
        if value.startswith("муж") or value in {"м", "male", "man", "m"}:
            return "male"
        return None

    @staticmethod
    def _electrolyte_patient_value(patient, key: str):
        if patient is None:
            return None
        if isinstance(patient, dict):
            return patient.get(key)
        return getattr(patient, key, None)

    def _electrolyte_context_weight_kg(self, admission_id: int) -> float | None:
        db = getattr(getattr(self.service, "patient_dao", None), "db", None)
        if db is None:
            db = getattr(getattr(self.service, "orders_dao", None), "db", None)
        if db is None or not hasattr(db, "fetch_one_remcard"):
            return None

        try:
            row = db.fetch_one_remcard(
                "SELECT intake_extra_json FROM admissions WHERE id = ?",
                (int(admission_id),),
            )
            payload = self._electrolyte_json_from_row(row, "intake_extra_json")
            weight = self._electrolyte_positive_float((payload or {}).get("weight_kg"))
            if weight is not None:
                return weight
        except Exception as exc:
            logger.warning("Electrolyte calculator: failed to read RAO transfer weight: %s", exc)

        try:
            row = db.fetch_one_remcard(
                """
                SELECT weight_kg
                FROM operation_cases
                WHERE future_rao_admission_id = ?
                  AND weight_kg IS NOT NULL
                ORDER BY id DESC
                LIMIT 1
                """,
                (int(admission_id),),
            )
            weight = self._electrolyte_positive_float(self._electrolyte_row_value(row, "weight_kg"))
            if weight is not None:
                return weight
        except Exception:
            pass

        try:
            row = db.fetch_one_remcard(
                """
                SELECT weight_kg
                FROM operation_cases
                WHERE admission_id = ?
                  AND weight_kg IS NOT NULL
                ORDER BY id DESC
                LIMIT 1
                """,
                (int(admission_id),),
            )
            return self._electrolyte_positive_float(self._electrolyte_row_value(row, "weight_kg"))
        except Exception as exc:
            logger.warning("Electrolyte calculator: failed to read operation weight: %s", exc)
            return None

    @staticmethod
    def _electrolyte_json_from_row(row, key: str) -> dict:
        raw = DoctorRemCardWidget._electrolyte_row_value(row, key)
        if not raw:
            return {}
        try:
            value = json.loads(str(raw))
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _electrolyte_row_value(row, key: str):
        if row is None:
            return None
        if isinstance(row, dict):
            return row.get(key)
        try:
            return row[key]
        except Exception:
            return None

    @staticmethod
    def _electrolyte_positive_float(value) -> float | None:
        if value in (None, ""):
            return None
        try:
            number = float(str(value).replace(",", "."))
        except Exception:
            return None
        return number if number > 0 else None

    def _electrolyte_context_last_24h_diuresis(self, patient, admission_id: int) -> float | None:
        admission_dt = getattr(patient, "admission_datetime", None) if patient is not None else None
        if admission_dt is None:
            return None
        now = datetime.now()
        try:
            if (now - admission_dt).total_seconds() < 24 * 3600:
                return None
        except Exception:
            return None

        fluid_service = getattr(self.service, "fluid_service", None)
        if fluid_service is None or not hasattr(fluid_service, "get_fluids_in_bounds"):
            return None
        try:
            from datetime import timedelta

            fluids = fluid_service.get_fluids_in_bounds(admission_id, now - timedelta(hours=24), now)
            return round(sum(float(getattr(fluid, "urine", 0.0) or 0.0) for fluid in fluids or []), 1)
        except Exception as exc:
            logger.warning("Electrolyte calculator: failed to load 24h diuresis: %s", exc)
            return None

    def on_bonus_clicked(self):
        try:
            from rem_card.app.runtime_paths import resolve_baza_dir
            from rem_card.ui.shared.minigames.bonus_dialog import BonusDialog

            dialog = BonusDialog(role="doctor", data_root_provider=resolve_baza_dir, parent=self)
            dialog.exec()
        except Exception as exc:
            CustomMessageBox.warning(self, "Бонус", f"Не удалось открыть бонус:\n{exc}")

    def on_global_archive_clicked(self):
        if hasattr(self, "content_stack"):
            self.content_stack.setCurrentWidget(self.layout_manager)
        self._exit_archive_read_only_mode()
        self._card_return_mode = None
        self._card_opened_from_global_archive = False
        self.layout_manager.set_patient_selection_mode("archive")
        self._wire_dynamic_views()
        self.layout_manager.bottom_row.hide()

    def _archive_patient_edit_service(self):
        db_manager = getattr(getattr(self._primary_service, "orders_dao", None), "db", None)
        if db_manager is None:
            raise RuntimeError("Сервис базы данных недоступен.")
        from rem_card.services.patient_bed_management import PatientBedManagementService

        return PatientBedManagementService(
            db_manager,
            data_service=getattr(self._primary_service, "data_service", None),
        )

    def on_patient_edit_requested_from_archive(self, patient):
        if getattr(patient, "is_external_archive", False):
            CustomMessageBox.information(
                self,
                "Только просмотр",
                "Запись прошлых периодов доступна только для просмотра.",
            )
            return

        try:
            admission_id = int(getattr(patient, "source_admission_id", None) or patient.id)
        except Exception:
            CustomMessageBox.warning(self, "Ошибка", "Не удалось определить госпитализацию пациента.")
            return

        try:
            edit_service = self._archive_patient_edit_service()
            patient_record, admission_record = edit_service.get_patient_with_admission(admission_id)
            if not patient_record or not admission_record:
                CustomMessageBox.warning(self, "Ошибка", "Карточка пациента не найдена.")
                return

            bed_number = getattr(admission_record, "bed_number", None)
            if bed_number is None:
                CustomMessageBox.warning(self, "Ошибка", "У карточки пациента не указан номер койки.")
                return

            from rem_card.ui.patient_bed_management.patient_form import PatientForm

            dialog = PatientForm(
                edit_service,
                int(bed_number),
                patient_record,
                admission_record,
                self,
            )
            try:
                result = dialog.exec()
            finally:
                dialog.deleteLater()

            if int(result) == int(QDialog.Accepted):
                self._refresh_after_archive_patient_edit(admission_id)
        except Exception as exc:
            logger.error("Failed to edit archived patient card: %s", exc, exc_info=True)
            CustomMessageBox.warning(self, "Ошибка", f"Не удалось открыть редактирование карточки:\n{exc}")

    def _refresh_after_archive_patient_edit(self, admission_id: int):
        archive_widget = getattr(self.layout_manager, "archive_widget", None)
        if archive_widget is not None:
            archive_widget.load_data()

        data_service = getattr(self._primary_service, "data_service", None)
        if data_service:
            try:
                data_service.request_immediate_refresh(force_emit=True)
            except Exception as exc:
                logger.warning("Failed to request refresh after archive patient edit: %s", exc)

        if self.admission_id and int(self.admission_id) == int(admission_id) and not self._archive_read_only_mode:
            try:
                self.force_reload_all()
            except Exception as exc:
                logger.warning("Failed to refresh opened card after archive patient edit: %s", exc, exc_info=True)

    def on_patient_selected_from_archive(self, patient):
        self._card_return_mode = "archive"
        self._card_opened_from_global_archive = True
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
                self.layout_manager.sync_bottom_row_visibility_to_current_tab()
            except Exception as exc:
                logger.error("Failed to open external archived card: %s", exc, exc_info=True)
                CustomMessageBox.warning(self, "Ошибка", f"Не удалось открыть архивную карту:\n{exc}")
                self._exit_archive_read_only_mode()
                self._card_return_mode = None
                self._card_opened_from_global_archive = False
            return
        self._exit_archive_read_only_mode()
        target_date = self._resolve_archive_open_date(patient.id, fallback_patient=patient)
        self.load_patient_card(patient.id, target_date, balance_patient_period_manual_mode=True)
        self._prime_patient_header_from_w1(patient, target_date)
        self.layout_manager.set_patient_selection_mode("card")
        self.layout_manager.sync_bottom_row_visibility_to_current_tab()

    def on_patient_selected_from_list(self, patient, action_type):
        self._exit_archive_read_only_mode()
        self._card_return_mode = None
        self._card_opened_from_global_archive = False
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
        elif action_type == "plan":
            self._open_or_create_plan_card(patient.id, patient=patient)
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
        controller.run_daily_report(self.admission_id, self._daily_report_reference_date())
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
                card_exists, yest_exists, plan_card_available = self._sector_4v_button_state(snapshot)
                self.layout_manager.sector_4v.set_buttons_state(
                    card_exists,
                    yest_exists,
                    plan_card_available,
                )
                self.update_latest_indicators()
                self._apply_archive_read_only_state()
            if hasattr(self.layout_manager, "set_current_status_dto"):
                self.layout_manager.set_current_status_dto(snapshot.get("status"))
            self.layout_manager.refresh_current_status()
        except Exception as e:
            from ...app.logger import logger
            logger.error(f"Error updating patient info in sector 4b/4v: {e}")

    def _update_emergency_notice_sector(self, snapshot=None):
        layout = getattr(self, "layout_manager", None)
        sector = getattr(layout, "sector_7vit_b", None) if layout is not None else None
        if sector is None:
            return
        try:
            loaded_from_service = False
            if hasattr(sector, "set_context") and self.admission_id:
                loaded_from_service = bool(sector.set_context(self.service, self.admission_id, self._current_date))
                if not loaded_from_service and hasattr(sector, "refresh"):
                    loaded_from_service = bool(sector.refresh())
            patient = (snapshot or self._card_snapshot_cache or {}).get("patient")
            has_draft = bool(getattr(sector, "has_unsaved_changes", lambda: False)())
            if not loaded_from_service and not has_draft and patient and hasattr(sector, "set_notice_data"):
                sector.set_notice_data(
                    getattr(patient, "emergency_notice_number", "") or "",
                    getattr(patient, "emergency_notice_entered_at", None),
                )
            if hasattr(sector, "set_forced_read_only"):
                sector.set_forced_read_only(bool(self._archive_read_only_mode))
        except Exception as exc:
            logger.warning("Failed to update emergency notice sector (doctor): %s", exc, exc_info=True)

    def refresh_data(self, show_empty_message=False):
        self._ensure_card_widgets_initialized()
        self._request_card_snapshot(show_empty_message=show_empty_message)

    def show_beds_mode(self):
        if self._is_closing:
            return
        self.admission_id = None
        layout = getattr(self, "layout_manager", None)
        if layout is not None and hasattr(layout, "set_patient_selection_mode"):
            layout.current_admission_id = None
            layout.set_patient_selection_mode("beds")
        if self._full_layout_created and layout is not None and hasattr(layout, "bottom_row"):
            layout.bottom_row.show()

    def reset_to_beds(self):
        self.show_beds_mode()

    def refresh_w1(self):
        layout = getattr(self, "layout_manager", None)
        beds_widget = getattr(layout, "beds_selection_widget", None)
        if beds_widget is not None and hasattr(beds_widget, "refresh"):
            beds_widget.refresh(queue_if_running=False)
        sector = getattr(layout, "sector_w1a", None)
        if sector is not None:
            if hasattr(sector, "set_service"):
                sector.set_service(self.service)
            if hasattr(sector, "refresh_data"):
                sector.refresh_data()

    def shutdown(self):
        self._is_closing = True
        self._shutdown_snapshot_worker()
        if hasattr(self, "chart") and self.chart and hasattr(self.chart, "shutdown"):
            self.chart.shutdown()
        if hasattr(self, "_balance_update_timer"):
            self._balance_update_timer.stop()
        if hasattr(self, "_add_patient_lock_watch_timer"):
            self._add_patient_lock_watch_timer.stop()
        self._disconnect_monitor()
        self._release_add_patient_lock()
        if getattr(self, "layout_manager", None) is getattr(self, "_w1_shell", None):
            if hasattr(self.layout_manager, "shutdown"):
                self.layout_manager.shutdown()
        else:
            if hasattr(self.layout_manager, "beds_selection_widget") and hasattr(self.layout_manager.beds_selection_widget, "shutdown"):
                self.layout_manager.beds_selection_widget.shutdown()
            if hasattr(self.layout_manager, 'orders_widget') and hasattr(self.layout_manager.orders_widget, "shutdown"):
                self.layout_manager.orders_widget.shutdown()
            if hasattr(self.layout_manager, "nurse_orders_manager") and hasattr(self.layout_manager.nurse_orders_manager, "shutdown"):
                self.layout_manager.nurse_orders_manager.shutdown()
            if hasattr(self.layout_manager, "sector_w1a") and hasattr(self.layout_manager.sector_w1a, "shutdown"):
                self.layout_manager.sector_w1a.shutdown()
        self._close_archive_readonly_manager()
        self._close_operblock_archive_viewer()

    def closeEvent(self, event):
        self.shutdown()
        if event is not None: super().closeEvent(event)

