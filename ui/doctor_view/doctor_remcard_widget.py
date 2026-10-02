from datetime import datetime

from PySide6.QtCore import QTimer, Signal, Qt
from PySide6.QtWidgets import QStackedWidget, QVBoxLayout, QWidget

from rem_card.app.logger import logger
from rem_card.ui.shared.balance_snapshot_sync import BalanceSnapshotSync, show_balance_sync_status

from .card_features.archive_context import DoctorArchiveContextMixin
from .card_features.card_actions import DoctorCardActionsMixin
from .card_features.constants import (
    CARD_UI_PREWARM_DELAY_MS,
    CARD_UI_PREWARM_ENABLED,
    JOURNAL_PREWARM_DELAY_MS,
    JOURNAL_PREWARM_ENABLED,
    PATIENT_PREVIEW_ICON_PREWARM_DELAY_MS,
)
from .card_features.infrastructure import DoctorInfrastructureMixin
from .card_features.layout import DoctorLayoutMixin
from .card_features.navigation import DoctorNavigationMixin
from .card_features.patient_card import DoctorPatientCardMixin
from .card_features.live_sync import DoctorLiveSyncMixin
from .card_features.snapshot_hydration import DoctorSnapshotHydrationMixin

class DoctorRemCardWidget(
    DoctorNavigationMixin,
    DoctorCardActionsMixin,
    DoctorLayoutMixin,
    DoctorPatientCardMixin,
    DoctorArchiveContextMixin,
    DoctorLiveSyncMixin,
    DoctorSnapshotHydrationMixin,
    DoctorInfrastructureMixin,
    QWidget,
):
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
        self._bound_archive_widget = None
        self._bound_admin_widget = None
        self._orders_widget_signals_bound = False
        self._nurse_orders_balance_signals_bound = False
        self.report_controller = None
        self._card_ui_prewarm_started = False
        self._card_ui_prewarm_done = False
        self._card_ui_prewarmer = None
        self._card_sector_preparation = None
        self._card_ui_prewarm_failed = False
        self._chart_init_pending = False
        self._last_applied_card_snapshot_signature = None
        self._last_applied_chart_signature = None
        self._journal_prewarm_started = False
        self._journal_prewarm_done = False
        self._selection_mode = "beds"
        from rem_card.ui.shared.doctor_navigation_diagnostics import DoctorNavigationDiagnostics

        self._navigation_diagnostics = DoctorNavigationDiagnostics(self)
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
        self._burn_patient_hint = None
        self._balance_runtime_cache = None
        self._balance_runtime_provisional = False
        self._balance_engine_worker = None
        self._balance_engine_request = None
        self._balance_engine_generation = 0
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
        self._card_state_refresh_pending = False
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
        self._balance_snapshot_sync = BalanceSnapshotSync(
            self,
            context_provider=self._balance_snapshot_context,
            load_snapshot=self._load_balance_snapshot_job,
            apply_snapshot=self._apply_authoritative_balance_snapshot,
            overlay_sequence_provider=self._balance_mark_override_sequence,
            role="doctor",
            delay_ms=self._balance_update_delay_ms,
            status_callback=lambda message: show_balance_sync_status(getattr(self, "layout_manager", None), message),
        )

        # Кнопка не опрашивает сетевой lock в фоне: проверка выполняется только при нажатии.
        self._add_patient_lock_watch_timer = None
        QTimer.singleShot(0, self._refresh_add_patient_button_lock_state)
        if CARD_UI_PREWARM_ENABLED:
            QTimer.singleShot(CARD_UI_PREWARM_DELAY_MS, self._schedule_card_ui_prewarm)
        if JOURNAL_PREWARM_ENABLED:
            QTimer.singleShot(JOURNAL_PREWARM_DELAY_MS, self._schedule_journal_prewarm)
        QTimer.singleShot(
            PATIENT_PREVIEW_ICON_PREWARM_DELAY_MS,
            self._preload_patient_preview_icons,
        )

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
        main_layout.setContentsMargins(5, 0, 5, 0)
        self.content_stack = QStackedWidget(self)

        from rem_card.ui.shared.role_entry_preload import take_prepared_entry
        prepared = take_prepared_entry(
            "doctor", patient_service=self.patient_service,
            remcard_service=self.service, operblock_service=self.operblock_service,
            parent=self.content_stack,
        )
        self._w1_shell = prepared[0] if prepared else LightweightW1Shell(
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
        self.sector8_panel = prepared[1] if prepared else Sector8Panel()
        self.btn_back = self.sector8_panel.btn_back
        self.btn_settings = self.sector8_panel.btn_settings
        self.btn_exit = self.sector8_panel.btn_exit

        self.btn_exit.clicked.connect(self.on_exit_clicked)
        self.btn_back.clicked.connect(self.on_back_clicked)
        self.sector8_panel.roles_clicked.connect(self._request_role_exit)
        self.btn_settings.clicked.connect(self.on_settings_clicked)
        self.sector8_panel.archive_clicked.connect(self.on_global_archive_clicked)
        self.sector8_panel.refresh_clicked.connect(self.on_refresh_beds_clicked)
        self.sector8_panel.calculations_clicked.connect(self.on_calculations_clicked)
        self.sector8_panel.add_patient_clicked.connect(self.on_add_patient_clicked)
        self.sector8_panel.user_report_clicked.connect(self.on_user_report_clicked)
        self.sector8_panel.user_reports_clicked.connect(self.on_user_reports_clicked)

        if hasattr(self.layout_manager, 'beds_selection_widget'):
            self.layout_manager.beds_selection_widget.patient_selected.connect(self.on_patient_selected_from_list)

        if hasattr(self.layout_manager, 'sector_8'):
            self.layout_manager.sector_8.set_content(self.sector8_panel)
        if hasattr(self.layout_manager, "selection_mode_changed"):
            self.layout_manager.selection_mode_changed.connect(self._on_selection_mode_changed)
            self._on_selection_mode_changed(getattr(self.layout_manager, "current_mode", "beds"))
        self._apply_burn_calculator_button_state()
        QTimer.singleShot(0, self._sync_roles_action_availability)

        # Динамические W1-экраны shell подключаются без создания полной карты.
        self._wire_dynamic_views()

    def shutdown(self):
        self._is_closing = True
        self._balance_engine_generation += 1
        balance_engine_worker = self._balance_engine_worker
        self._balance_engine_worker = None
        self._balance_engine_request = None
        if balance_engine_worker is not None:
            try:
                balance_engine_worker.quit()
            except Exception:
                pass
        prewarmer = getattr(self, "_card_ui_prewarmer", None)
        if prewarmer is not None:
            prewarmer.cancel(reason="role_shutdown")
        diagnostics = getattr(self, "_navigation_diagnostics", None)
        if diagnostics is not None:
            diagnostics.close()
        self._balance_snapshot_sync.shutdown()
        self._shutdown_snapshot_worker()
        sector_ivl = getattr(getattr(self, "layout_manager", None), "sector_ivl", None)
        if sector_ivl is not None and hasattr(sector_ivl, "shutdown"):
            sector_ivl.shutdown()
        sector_notice = getattr(getattr(self, "layout_manager", None), "sector_7vit_b", None)
        if sector_notice is not None and hasattr(sector_notice, "shutdown"):
            sector_notice.shutdown()
        if hasattr(self, "chart") and self.chart and hasattr(self.chart, "shutdown"):
            self.chart.shutdown()
        if hasattr(self, "_balance_update_timer"):
            self._balance_update_timer.stop()
        if hasattr(self, "_add_patient_lock_watch_timer"):
            if self._add_patient_lock_watch_timer:
                self._add_patient_lock_watch_timer.stop()
        panel = getattr(self, "sector8_panel", None)
        if panel is not None and hasattr(panel, "shutdown"):
            panel.shutdown()
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
