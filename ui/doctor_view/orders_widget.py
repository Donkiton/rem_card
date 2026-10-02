"""Doctor orders widget composition root."""

import os

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import QWidget

from rem_card.ui.doctor_view.order_features.administration_actions import AdministrationActionsMixin
from rem_card.ui.doctor_view.order_features.change_writes import ChangeWriteCoordinationMixin
from rem_card.ui.doctor_view.order_features.constants import ORDERS_FORCED_RELOAD_COOLDOWN_MS
from rem_card.ui.doctor_view.order_features.draft_management import DraftManagementMixin
from rem_card.ui.doctor_view.order_features.interface import OrdersInterfaceMixin
from rem_card.ui.doctor_view.order_features.order_state import OrderStateMixin
from rem_card.ui.doctor_view.order_features.refresh_coordination import RefreshCoordinationMixin
from rem_card.ui.doctor_view.order_features.snapshot_application import SnapshotApplicationMixin
from rem_card.ui.doctor_view.order_features.snapshot_workers import SnapshotWorkerLifecycleMixin


class OrdersWidget(
    DraftManagementMixin,
    SnapshotWorkerLifecycleMixin,
    OrderStateMixin,
    RefreshCoordinationMixin,
    SnapshotApplicationMixin,
    ChangeWriteCoordinationMixin,
    AdministrationActionsMixin,
    OrdersInterfaceMixin,
    QWidget,
):
    draftStatusChanged = Signal(bool)
    administrationStatusChanged = Signal(bool)
    ordersPresenceChanged = Signal(bool)
    localBalanceChanged = Signal()
    balanceSnapshotRequired = Signal()
    committedOrdersBalanceReady = Signal(object)
    localDraftResolutionFinished = Signal(bool)
    _LOCAL_SILENT_FORCE_PREFIXES = (
        "orders_add_input:",
        "orders_add_cvp:",
        "orders_edit_input:",
        "orders_left_click:",
        "orders_middle_click:",
        "orders_right_click:",
        "orders_finalize:",
    )
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
        self._is_closing = False
        self._snapshot_worker = None
        self._snapshot_pending = False
        self._snapshot_force_pending = False
        self._snapshot_pending_source = "refresh"
        self._snapshot_pending_priority = "MEDIUM"
        self._snapshot_pending_reason = None
        self._active_request_context_key = None
        self._active_request_force = False
        self._active_request_priority = "MEDIUM"
        self._active_request_seq = 0
        self._active_request_id = ""
        self._active_request_generation = 0
        self._active_request_source = "refresh"
        self._active_request_started_monotonic = 0.0
        self._forced_reload_cooldown_ms = ORDERS_FORCED_RELOAD_COOLDOWN_MS
        self._forced_reload_recent = {}
        self._forced_reload_active_key = None
        self._forced_reload_pending_key = None
        self._forced_reload_after_guard_key = None
        self._forced_reload_after_guard_reason = None
        self._active_snapshot_worker_state = {}
        self._retired_snapshot_worker_states = {}
        self._post_finalize_retry_count = 0
        self._post_finalize_retry_after_cancel = False
        self._post_finalize_retry_context_hash = None
        self._refresh_status_label = None
        self._snapshot_stale = False
        self._snapshot_seq = 0
        self._last_applied_snapshot_signature = None
        self._cached_has_drafts = False
        self._cached_has_administrations = False
        self._cached_has_orders = False
        self._admin_only_snapshot_until = 0.0
        self._orders_click_seq = 0
        self._pending_admin_write_count = 0
        self._balance_mark_overrides = {}
        self._balance_mark_override_seq = 0
        self._pending_admin_cell_write_keys = set()
        self._recent_admin_cell_clicks = {}
        self._local_cell_draft_guard = False
        self._local_cell_draft_guard_signatures = {}
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
        self._post_finalize_watchdog_timer = QTimer(self)
        self._post_finalize_watchdog_timer.setSingleShot(True)
        self._post_finalize_watchdog_timer.timeout.connect(self._on_post_finalize_snapshot_watchdog)
        self._perf_enabled = os.getenv("REMCARD_PROFILE_ORDERS_CLICK", "0") == "1"
        self._perf_next_click_id = 0
        self._perf_clicks = {}
        self._pending_reorder_order_ids = []
        self._draft_baseline_snapshot = None
        self._draft_baseline_admin_map = {}
        self._local_draft_dirty_order_ids = set()
        self._local_draft_dirty_admin_keys = set()
        self._local_deleted_orders = {}
        self._local_draft_save_pending = False
        self._legacy_central_draft_detected = False
        self._next_local_order_id = -1
        self._next_local_admin_id = -1
        self._row_drag_state = None
        self._row_drag_ghost = None
        self._row_drag_indicator = None
        if not self._defer_ui:
            self.setup_ui()
        
        # Таймер для обновления маркера "Сейчас"
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_now_marker)
        self.timer.start(60000)
