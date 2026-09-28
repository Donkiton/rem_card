from __future__ import annotations

from PySide6.QtCore import QEventLoop
from PySide6.QtCore import QTimer
from PySide6.QtCore import Qt
from PySide6.QtCore import Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import QDialog
from PySide6.QtWidgets import QFrame
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QPushButton
from PySide6.QtWidgets import QStackedWidget
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget
from datetime import datetime
from rem_card.app import operblock_startup_metrics
from rem_card.services.operblock_anesthesia_prep import invalidate_start_anesthesia_options_cache
from rem_card.services.operblock_anesthesia_types import load_operblock_anesthesia_types
from rem_card.services.operblock_anesthesia_types import save_operblock_anesthesia_types
from rem_card.services.operblock_medication_presets import save_operblock_medication_presets
from rem_card.services.operblock_quick_orders import load_operblock_quick_orders
from rem_card.services.operblock_service import OperBlockService
from rem_card.services.operblock_team import load_operblock_team
from rem_card.services.operblock_team import save_operblock_team
from rem_card.ui.rem_card_sectors.sector_8 import Sector8
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.shared.loading_overlay import hide_app_loading
from rem_card.ui.shared.loading_overlay import show_app_loading
from rem_card.ui.shared.pdf_opener import open_pdf_file
from rem_card.ui.shared.vitals_widget import VitalsWidget
from rem_card.ui.styles.theme import BG_MAIN
from rem_card.ui.styles.theme import TEXT_PRIMARY
from rem_card.ui.styles.theme_runtime import set_widget_style
from typing import Any
import time
import weakref
from rem_card.ui.operblock_view.operblock_helpers import (
    _normalize_operblock_table_filter,
    _operblock_table_display_name,
    _safe_int,
)
from rem_card.ui.operblock_view.operblock_preset_dialogs import (
    OperBlockMedicationPresetsDialog,
)
from rem_card.ui.operblock_view.operblock_settings_dialogs import (
    OperBlockAnesthesiaTypesDialog,
    OperBlockSettingsDialog,
    OperBlockTeamDialog,
)
from rem_card.ui.operblock_view.operblock_visual_primitives import (
    ElidedTooltipLabel,
    OPERBLOCK_INITIAL_CHART_HOURS,
    OperBlockSector8Panel,
    SECTOR_BODY_STYLE,
    SECTOR_HEADER_STYLE,
)
from rem_card.ui.operblock_view.operblock_vitals_adapter import (
    OperBlockVitalsServiceAdapter,
)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rem_card.ui.operblock_view.operblock_chart_widget import OperBlockChartWidget


from rem_card.ui.operblock_view.features.refresh import OperBlockRefreshMixin
from rem_card.ui.operblock_view.features.archive import OperBlockArchiveMixin
from rem_card.ui.operblock_view.features.board_cards import OperBlockBoardCardsMixin
from rem_card.ui.operblock_view.features.protocol_layout import OperBlockProtocolLayoutMixin
from rem_card.ui.operblock_view.features.stages import OperBlockStagesMixin
from rem_card.ui.operblock_view.features.orders_layout import OperBlockOrdersLayoutMixin
from rem_card.ui.operblock_view.features.board_details import OperBlockBoardDetailsMixin
from rem_card.ui.operblock_view.features.case_actions import OperBlockCaseActionsMixin
from rem_card.ui.operblock_view.features.protocol_state import OperBlockProtocolStateMixin
from rem_card.ui.operblock_view.features.quick_orders import OperBlockQuickOrdersMixin
from rem_card.ui.operblock_view.features.orders_rendering import OperBlockOrdersRenderingMixin
from rem_card.ui.operblock_view.features.medication_groups import OperBlockMedicationGroupsMixin
from rem_card.ui.operblock_view.features.infusion_cards import OperBlockInfusionCardsMixin
from rem_card.ui.operblock_view.features.infusion_actions import OperBlockInfusionActionsMixin
from rem_card.ui.operblock_view.features.order_history import OperBlockOrderHistoryMixin
from rem_card.ui.operblock_view.features.order_actions import OperBlockOrderActionsMixin
from rem_card.ui.operblock_view.features.local_updates import OperBlockLocalUpdatesMixin
from rem_card.ui.operblock_view.features.write_coordinator import OperBlockWriteCoordinatorMixin


class OperBlockMainWidget(
    OperBlockRefreshMixin,
    OperBlockArchiveMixin,
    OperBlockBoardCardsMixin,
    OperBlockProtocolLayoutMixin,
    OperBlockStagesMixin,
    OperBlockOrdersLayoutMixin,
    OperBlockBoardDetailsMixin,
    OperBlockCaseActionsMixin,
    OperBlockProtocolStateMixin,
    OperBlockQuickOrdersMixin,
    OperBlockOrdersRenderingMixin,
    OperBlockMedicationGroupsMixin,
    OperBlockInfusionCardsMixin,
    OperBlockInfusionActionsMixin,
    OperBlockOrderHistoryMixin,
    OperBlockOrderActionsMixin,
    OperBlockLocalUpdatesMixin,
    OperBlockWriteCoordinatorMixin,
    QWidget,
):
    view_back_requested = Signal()

    def __init__(
        self,
        patient_service,
        remcard_service,
        operblock_service: OperBlockService | None = None,
        parent=None,
        *,
        table_code: str | None = None,
        view_only: bool = False,
    ):
        super().__init__(parent)
        self.patient_service = patient_service
        self.remcard_service = remcard_service
        self.data_service = getattr(remcard_service, "data_service", None)
        if operblock_service is None:
            raise RuntimeError("OperBlockService не передан в OperBlockMainWidget.")
        self.operblock_service = operblock_service
        self.operblock_vitals_service = OperBlockVitalsServiceAdapter(remcard_service, operblock_service)
        self._table_filter_code = _normalize_operblock_table_filter(table_code)
        self._table_filter_name = _operblock_table_display_name(self._table_filter_code)
        self._view_only_mode = bool(view_only)
        self._is_closing = False
        self._board_hash = ""
        self._board_refresh_worker = None
        self._board_refresh_pending: dict[str, Any] | None = None
        self._protocol_hash = ""
        self._protocol_refresh_worker = None
        self._protocol_refresh_pending: dict[str, Any] | None = None
        self._start_anesthesia_prep_worker = None
        self._start_anesthesia_prep_generation = 0
        self._start_anesthesia_prep_pending = False
        self._protocol_tab_ready_pending = False
        self._chart_module_preload_worker = None
        self._chart_module_preloaded = False
        self._chart_module_preload_failed = False
        self._current_operation_case_id: int | None = None
        self._current_admission_id: int | None = None
        self._current_operation_start: datetime | None = None
        self._current_operation_end: datetime | None = None
        self._current_case_active = False
        self._current_operation_has_vitals = False
        self._archive_return_operation_case_id: int | None = None
        self._protocol_opened_from_archive = False
        self._current_stage_state: dict = {}
        self._current_anesthesia_start: datetime | None = None
        self._current_anesthesia_end: datetime | None = None
        self._current_surgery_start: datetime | None = None
        self._current_surgery_end: datetime | None = None
        self._current_anesthesia_active = False
        self._current_surgery_active = False
        self._current_anesthesia_assistance_type = ""
        self._current_operation_name = ""
        self._current_protocol_display = ""
        self._current_protocol_date = datetime.now()
        self._vitals_context_key: tuple[int, int, str] | None = None
        self._refresh_generation = 0
        self._write_pending = False
        self._opblock_idle_last_activity_monotonic = time.monotonic()
        self._opblock_idle_period_reported = False
        self._active_opblock_action: dict[str, Any] | None = None
        self._last_opblock_action: dict[str, Any] | None = None
        self._active_foreground_resume_lease: dict[str, Any] | None = None
        self._table_cards: dict[str, QFrame] = {}
        self._board_card_hashes: dict[str, str] = {}
        self._board_card_states: dict[str, dict] = {}
        self._board_photo_thumbnail_cache: dict[tuple, QPixmap] = {}
        self._quick_order_templates: list[dict] = []
        self._medication_presets: list[dict] = []
        self._quick_orders_data_loaded = False
        self._preset_search_text = ""
        self._preset_kind_filter = "bolus"
        self._quick_order_filter_buttons: list[dict] = []
        self._quick_order_filter_keys: list[str] = []
        self._quick_order_drug_group_label_by_key: dict[str, str] = {}
        self._quick_order_search_haystack_by_preset_id: dict[str, str] = {}
        self._orders_filter_kind = "all"
        self._orders_hide_deleted = True
        self._quick_order_buttons: list[QPushButton] = []
        self._quick_order_card_widgets: dict[str, QWidget] = {}
        self._quick_order_card_signatures: dict[str, str] = {}
        self._quick_order_visible_preset_ids: list[str] = []
        self._quick_order_drag_source_id: str | None = None
        self._quick_order_drag_placeholder: QWidget | None = None
        self._quick_order_drag_order: list[str] = []
        self._quick_order_drag_committed = False
        self._pending_quick_orders_scroll_state: dict | None = None
        self._order_action_buttons: list[QPushButton] = []
        self._infusion_action_buttons: list[QPushButton] = []
        self._orders_force_top_on_next_apply = False
        self._current_orders_rows: list[dict] = []
        self._current_timeline_snapshot: dict | None = None
        self._current_chart_vitals: list[Any] = []
        self._pending_orders_snapshot: dict | None = None
        self._route_only_write_suppressions: dict[tuple[int, int], float] = {}
        self._local_write_refresh_suppressions: dict[str, dict] = {}
        self._collapsed_order_group_keys: set[str] = set()
        self._orders_render_signature = ""
        self._orders_source_signature = ""
        self._rendered_medication_group_widgets: dict[str, QWidget] = {}
        self._rendered_medication_group_signatures: dict[str, str] = {}
        self._rendered_medication_group_order: list[str] = []
        self._rendered_order_detail_labels: dict[int, ElidedTooltipLabel] = {}
        self._rendered_medication_group_total_labels: dict[str, ElidedTooltipLabel] = {}
        self._active_infusions_render_signature = ""
        self._rendered_active_infusion_widgets: dict[str, QWidget] = {}
        self._rendered_active_infusion_signatures: dict[str, str] = {}
        self._rendered_active_infusion_order: list[str] = []
        self._active_infusions_empty_widget: QWidget | None = None
        self._orders_empty_widget: QWidget | None = None
        self._last_infusion_elapsed_refresh_minute = ""
        self._archive_cases: list[dict] = []
        self._archive_page_size = 50
        self._archive_current_page = 1
        self._archive_total_pages = 1
        self._archive_total_records = 0
        self._external_archive_viewer = None
        self._external_archive_db_manager = None
        self._role_launcher_mode = False
        self.protocol_page: QWidget | None = None
        self.archive_page: QWidget | None = None
        self.settings_page: QWidget | None = None
        self._vitals_tab_built = False
        self._orders_tab_built = False
        self._settings_return_page = "board"
        self._settings_return_operation_case_id: int | None = None
        self.content_stack: QStackedWidget | None = None
        self.vitals_chart: OperBlockChartWidget | None = None
        self.vitals_input: VitalsWidget | None = None
        self._creating_lazy_protocol_page = False
        self._creating_lazy_archive_page = False
        self._board_refresh_seq = 0
        self._board_refresh_count_before_ready = 0
        self._current_board_apply_metrics: dict | None = None
        self._operation_report_pdf_worker = None
        self._operation_report_pdf_buttons: list[tuple[Any, str, bool]] = []
        init_ui_started = operblock_startup_metrics.timer_start()
        try:
            self._init_ui()
        finally:
            operblock_startup_metrics.record_since("operblock_init_ui_ms", init_ui_started, source="operblock_widget")
        self._protocol_clock_timer = QTimer(self)
        self._protocol_clock_timer.timeout.connect(self._update_protocol_current_time_label)
        self._protocol_clock_timer.start(1000)
        self._update_protocol_current_time_label()
        self._connect_updates()

    def open_archive_protocol(self, operation_case_id: int):
        self._open_protocol(int(operation_case_id))

    def is_view_only_mode(self) -> bool:
        return bool(getattr(self, "_view_only_mode", False))

    def _show_operblock_loading(
        self,
        message: str | None,
        *,
        key: str,
        auto_hide_ms: int = 20000,
        process_events: bool = True,
    ) -> str | None:
        if not message:
            return None
        return show_app_loading(
            self,
            message,
            key=f"operblock-{key}:{id(self)}",
            auto_hide_ms=auto_hide_ms,
            process_events=process_events,
        )

    def _hide_operblock_loading(self, loading_key: str | None, *, delay_ms: int = 350) -> None:
        if loading_key:
            hide_app_loading(self, loading_key, delay_ms=delay_ms)

    def _pump_operblock_ui_events(self) -> None:
        app = QApplication.instance()
        if app is None:
            return
        app.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)

    def _apply_view_only_chrome_state(self):
        if not self.is_view_only_mode():
            return
        panel = getattr(self, "sector_8_panel", None)
        if panel is None:
            return
        for button_name in ("btn_archive", "btn_settings"):
            button = getattr(panel, button_name, None)
            if button is not None:
                button.setVisible(False)

    def _init_ui(self):
        from rem_card.ui.shared.workspace_background import FIXED_WORKSPACE_BACKGROUND
        background = 'transparent' if FIXED_WORKSPACE_BACKGROUND else BG_MAIN
        set_widget_style(self, f"QWidget {{ background-color: {background}; color: {TEXT_PRIMARY}; }}")
        root = QVBoxLayout(self)
        root.setContentsMargins(5, 0, 5, 0)
        root.setSpacing(0)

        self.sector_8 = Sector8()
        self.sector_8.setFixedHeight(38)
        self.sector_8.set_horizontal_frame_margins(3, 1)
        self.sector_8_panel = OperBlockSector8Panel()
        self.sector_8_panel.btn_archive.clicked.connect(self._show_operblock_archive)
        self.sector_8_panel.btn_refresh.clicked.connect(lambda: self.auto_refresh(force=True))
        self.sector_8_panel.btn_user_report.clicked.connect(self._open_user_report_dialog)
        self.sector_8_panel.btn_user_reports.clicked.connect(self._open_user_reports_dialog)
        self.sector_8_panel.btn_settings.clicked.connect(self._open_unified_settings)
        self.sector_8_panel.btn_back.clicked.connect(self.on_back_clicked)
        self.sector_8_panel.btn_roles.clicked.connect(self._request_role_exit)
        self.sector_8_panel.btn_exit.clicked.connect(lambda: self.window().close())
        self._apply_view_only_chrome_state()
        self.sector_8.set_content(self.sector_8_panel)
        root.addWidget(self.sector_8)

        self.stack = QStackedWidget()
        self.board_page = self._build_board_page()
        self.stack.addWidget(self.board_page)
        root.addWidget(self.stack, 1)
        self._set_protocol_chrome(False)
        QTimer.singleShot(0, self._sync_roles_action_availability)

    def _open_user_report_dialog(self):
        from rem_card.ui.shared.user_reports_dialog import UserReportDialog

        dialog = UserReportDialog(role="operblock", parent=self)
        dialog.submitted.connect(self._refresh_user_reports_count)
        dialog.exec()
        self._refresh_user_reports_count()

    def _open_user_reports_dialog(self):
        from rem_card.ui.shared.user_reports_dialog import UserReportsInboxDialog

        dialog = UserReportsInboxDialog(role="operblock", parent=self)
        dialog.reports_changed.connect(self._refresh_user_reports_count)
        dialog.exec()
        self._refresh_user_reports_count()

    def _refresh_user_reports_count(self):
        panel = getattr(self, "sector_8_panel", None)
        method = getattr(panel, "refresh_user_reports_count", None)
        if callable(method):
            method()

    def _ensure_protocol_page_created(self) -> bool:
        if self.protocol_page is not None:
            return True
        metric_started = operblock_startup_metrics.timer_start()
        self._creating_lazy_protocol_page = True
        try:
            self.protocol_page = self._build_protocol_page()
            self.stack.addWidget(self.protocol_page)
            self._update_protocol_current_time_label()
            self._apply_protocol_controls_state()
            return True
        finally:
            self._creating_lazy_protocol_page = False
            operblock_startup_metrics.record_since(
                "protocol_page_lazy_created_ms",
                metric_started,
                source="operblock_widget",
            )

    def _ensure_archive_page_created(self) -> bool:
        if self.archive_page is not None:
            return True
        metric_started = operblock_startup_metrics.timer_start()
        self._creating_lazy_archive_page = True
        try:
            self.archive_page = self._build_archive_page()
            self.stack.addWidget(self.archive_page)
            return True
        finally:
            self._creating_lazy_archive_page = False
            operblock_startup_metrics.record_since(
                "archive_page_lazy_created_ms",
                metric_started,
                source="operblock_widget",
            )

    def _ensure_settings_page_created(self) -> bool:
        if self.settings_page is not None:
            return True
        try:
            from rem_card.ui.admin_view.admin_main_widget import AdminMainWidget

            self.settings_page = AdminMainWidget(
                service=self.remcard_service,
                role="doctor",
                parent=self.stack,
                left_outer_margin=5,
            )
            self.stack.addWidget(self.settings_page)
            return True
        except Exception as exc:
            self.settings_page = None
            CustomMessageBox.warning(self, "Настройки", f"Не удалось открыть настройки: {exc}")
            return False

    def _sector(self, title: str) -> tuple[QWidget, QVBoxLayout]:
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        header = QLabel(title)
        header.setFixedHeight(28)
        header.setAlignment(Qt.AlignCenter)
        set_widget_style(header, SECTOR_HEADER_STYLE)
        body = QFrame()
        set_widget_style(body, SECTOR_BODY_STYLE)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        layout.addWidget(header)
        layout.addWidget(body, 1)
        return wrapper, body_layout

    def _open_unified_settings(self):
        if self._is_closing:
            return
        if self.is_view_only_mode():
            return
        action_info = self._start_opblock_action_diagnostics("operblock_open_settings")
        loading_key = self._show_operblock_loading(
            "Открытие настроек...",
            key="open-settings",
            auto_hide_ms=20000,
        )
        try:
            self._remember_settings_return_page()
            if not self._ensure_settings_page_created():
                return
            page = self.settings_page
            if page is None:
                return
            if hasattr(page, "set_print_context"):
                page.set_print_context(self.remcard_service, self._current_admission_id, datetime.now())
            if hasattr(page, "show_menu"):
                page.show_menu()
            self._set_protocol_chrome(True)
            self.stack.setCurrentWidget(page)
        finally:
            self._finish_opblock_action_diagnostics(action_info, "success")
            self._hide_operblock_loading(loading_key)

    def _remember_settings_return_page(self):
        current_widget = self.stack.currentWidget()
        if current_widget == self.settings_page:
            return
        self._settings_return_page = "board"
        self._settings_return_operation_case_id = None
        if current_widget == self.protocol_page and self._current_operation_case_id:
            self._settings_return_page = "protocol"
            self._settings_return_operation_case_id = int(self._current_operation_case_id)
        elif current_widget == self.archive_page:
            self._settings_return_page = "archive"
            return_case_id = int(getattr(self, "_archive_return_operation_case_id", 0) or 0)
            self._settings_return_operation_case_id = return_case_id or None

    def _on_settings_back_clicked(self):
        action_info = self._start_opblock_action_diagnostics("operblock_settings_back")
        page = self.settings_page
        try:
            if page is not None and hasattr(page, "go_back") and page.go_back():
                return
            self._return_from_settings()
        finally:
            self._finish_opblock_action_diagnostics(action_info, "success")

    def _return_from_settings(self):
        return_page = str(getattr(self, "_settings_return_page", "") or "board")
        return_case_id = int(getattr(self, "_settings_return_operation_case_id", 0) or 0)
        self._settings_return_page = "board"
        self._settings_return_operation_case_id = None
        if return_page == "protocol" and return_case_id:
            if self.protocol_page is not None and self._current_operation_case_id == return_case_id:
                self._set_protocol_chrome(True)
                self.stack.setCurrentWidget(self.protocol_page)
                self.refresh_protocol(
                    force=True,
                    loading_message="Возврат к протоколу операции...",
                )
                return
            self._open_protocol(return_case_id)
            return
        if return_page == "archive" and self.archive_page is not None:
            self._archive_return_operation_case_id = return_case_id or None
            self._set_protocol_chrome(True)
            self.stack.setCurrentWidget(self.archive_page)
            self.refresh_operblock_archive(
                force=True,
                loading_message="Возврат в архив оперблока...",
            )
            return
        self._show_board(loading_message="Возврат к операционной...")

    def _open_operblock_settings(self):
        if self._is_closing:
            return
        if self.is_view_only_mode():
            return
        dialog = OperBlockSettingsDialog(self)
        dialog.medications_button.clicked.connect(lambda: self._open_quick_orders_settings(dialog))
        dialog.anesthesia_types_button.clicked.connect(lambda: self._open_anesthesia_types_settings(dialog))
        dialog.team_button.clicked.connect(lambda: self._open_operblock_team_settings(dialog))
        dialog.exec()

    def _open_anesthesia_types_settings(self, dialog_parent: QWidget | None = None):
        if self.is_view_only_mode():
            return
        loading_key = self._show_operblock_loading(
            "Загрузка видов пособия...",
            key="anesthesia-types-settings",
            auto_hide_ms=20000,
        )
        try:
            try:
                items = load_operblock_anesthesia_types()
            except Exception as exc:
                CustomMessageBox.warning(self, "Виды пособия", f"Не удалось загрузить виды пособия: {exc}")
                return
        finally:
            self._hide_operblock_loading(loading_key, delay_ms=0)
        parent = dialog_parent if isinstance(dialog_parent, QWidget) else self
        dialog = OperBlockAnesthesiaTypesDialog(items, parent)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            save_operblock_anesthesia_types(dialog.items())
        except Exception as exc:
            CustomMessageBox.warning(self, "Виды пособия", f"Не удалось сохранить виды пособия: {exc}")
            return
        invalidate_start_anesthesia_options_cache()

    def _open_operblock_team_settings(self, dialog_parent: QWidget | None = None):
        if self._is_closing:
            return
        if self.is_view_only_mode():
            return
        loading_key = self._show_operblock_loading(
            "Загрузка опер. бригады...",
            key="team-settings",
            auto_hide_ms=20000,
        )
        try:
            try:
                items = load_operblock_team()
            except Exception as exc:
                CustomMessageBox.warning(self, "Опер. бригада", f"Не удалось загрузить опер. бригаду: {exc}")
                return
        finally:
            self._hide_operblock_loading(loading_key, delay_ms=0)
        parent = dialog_parent if isinstance(dialog_parent, QWidget) else self
        dialog = OperBlockTeamDialog(items, parent)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            save_operblock_team(dialog.items())
        except Exception as exc:
            CustomMessageBox.warning(self, "Опер. бригада", f"Не удалось сохранить опер. бригаду: {exc}")
            return
        invalidate_start_anesthesia_options_cache()

    def _open_quick_orders_settings(self, dialog_parent: QWidget | None = None):
        if self._is_closing or self._write_pending:
            return
        if self.is_view_only_mode():
            return
        loading_key = self._show_operblock_loading(
            "Загрузка настроек препаратов...",
            key="quick-orders-settings",
            auto_hide_ms=20000,
        )
        try:
            self._load_quick_orders_data()
        finally:
            self._hide_operblock_loading(loading_key, delay_ms=0)
        parent = dialog_parent if isinstance(dialog_parent, QWidget) else self
        dialog = OperBlockMedicationPresetsDialog(
            self._medication_presets,
            parent,
            save_handler=self._save_medication_presets_from_dialog,
        )
        dialog.exec()

    def _save_medication_presets_from_dialog(self, templates: list[dict]) -> list[dict]:
        result = save_operblock_medication_presets(templates)
        self._on_quick_orders_saved(result)
        return list(result or [])

    def _on_quick_orders_saved(self, result):
        self._write_pending = False
        self.sector_8_panel.btn_settings.setEnabled(True)
        self._medication_presets = list(result or [])
        try:
            self._quick_order_templates = load_operblock_quick_orders()
        except Exception:
            self._quick_order_templates = []
        self._quick_orders_data_loaded = True
        self._rebuild_quick_order_search_index()
        self._render_quick_orders()
        if self._current_operation_case_id:
            self._orders_force_top_on_next_apply = True
            self.refresh_protocol(force=True)

    def _on_quick_orders_save_error(self, exc: Exception):
        self._write_pending = False
        self.sector_8_panel.btn_settings.setEnabled(True)
        CustomMessageBox.warning(self, "Ошибка сохранения", str(exc))
        self._refresh_quick_orders(force_reload=True)

    def _build_operation_report_pdf(self, operation_case_id: int | None = None, *, trigger_button: QPushButton | None = None):
        case_id = _safe_int(operation_case_id) or _safe_int(self._current_operation_case_id)
        if not case_id:
            CustomMessageBox.warning(self, "Отчет за операцию", "Откройте протокол операции.")
            return
        worker = getattr(self, "_operation_report_pdf_worker", None)
        if worker is not None and worker.isRunning():
            CustomMessageBox.information(self, "Отчет за операцию", "PDF отчета уже формируется.")
            return
        try:
            pdf_path = self.operblock_service.build_operation_report_pdf_path(int(case_id))
        except Exception as exc:
            CustomMessageBox.critical(self, "Отчет за операцию", f"Не удалось подготовить путь PDF:\n{exc}")
            return
        from rem_card.ui.operblock_view.operblock_report_pdf_worker import OperBlockReportPdfWorker

        self._set_operation_report_buttons_busy(trigger_button)
        self._operation_report_pdf_worker = OperBlockReportPdfWorker(
            self.operblock_service,
            int(case_id),
            pdf_path,
            parent=self,
        )
        self._operation_report_pdf_worker.completed.connect(self._on_operation_report_pdf_ready)
        self._operation_report_pdf_worker.failed.connect(self._on_operation_report_pdf_error)
        self._operation_report_pdf_worker.finished.connect(self._clear_operation_report_pdf_worker)
        self._operation_report_pdf_worker.start()

    def _set_operation_report_buttons_busy(self, trigger_button: QPushButton | None = None) -> None:
        buttons: list[QPushButton] = []
        if trigger_button is not None:
            buttons.append(trigger_button)
        protocol_button = getattr(self, "report_button", None)
        if protocol_button is not None and all(button is not protocol_button for button in buttons):
            buttons.append(protocol_button)

        self._operation_report_pdf_buttons = []
        for button in buttons:
            try:
                self._operation_report_pdf_buttons.append((weakref.ref(button), button.text(), button.isEnabled()))
                button.setEnabled(False)
                button.setText(" Формирование PDF..." if button is protocol_button else "ФОРМИРОВАНИЕ PDF...")
            except RuntimeError:
                continue

    def _restore_operation_report_buttons(self) -> None:
        buttons = list(getattr(self, "_operation_report_pdf_buttons", []) or [])
        self._operation_report_pdf_buttons = []
        for button_ref, text, was_enabled in buttons:
            try:
                button = button_ref()
                if button is None:
                    continue
                button.setText(text)
                button.setEnabled(bool(was_enabled))
            except RuntimeError:
                continue

    def _on_operation_report_pdf_ready(self, pdf_path: str):
        self._restore_operation_report_buttons()
        open_pdf_file(pdf_path, parent=self)

    def _on_operation_report_pdf_error(self, message: str):
        self._restore_operation_report_buttons()
        CustomMessageBox.critical(self, "Отчет за операцию", f"Не удалось сформировать PDF отчета:\n{message}")

    def _clear_operation_report_pdf_worker(self):
        self._restore_operation_report_buttons()
        self._operation_report_pdf_worker = None

    def _show_board(self, *, loading_message: str | None = None):
        action_info = self._start_opblock_action_diagnostics("operblock_show_board")
        loading_key = self._show_operblock_loading(
            loading_message,
            key="show-board",
            auto_hide_ms=30000,
        )
        try:
            self._set_protocol_chrome(False)
            self.stack.setCurrentWidget(self.board_page)
            self._protocol_opened_from_archive = False
            self._archive_return_operation_case_id = None
            self._current_operation_case_id = None
            self._current_admission_id = None
            self._current_operation_start = None
            self._current_operation_end = None
            self._current_case_active = False
            self._current_operation_has_vitals = False
            self._current_stage_state = {}
            self._current_anesthesia_start = None
            self._current_anesthesia_end = None
            self._current_surgery_start = None
            self._current_surgery_end = None
            self._current_anesthesia_active = False
            self._current_surgery_active = False
            self._current_anesthesia_assistance_type = ""
            self._current_operation_name = ""
            self._current_protocol_display = ""
            self._update_protocol_title_label()
            self._update_operblock_staff_legend()
            self._vitals_context_key = None
            self._current_orders_rows = []
            self._current_timeline_snapshot = None
            self._current_chart_vitals = []
            self._pending_orders_snapshot = {"orders": []}
            self._apply_active_infusions()
            self.operblock_vitals_service.set_operation_context(
                operation_case_id=None,
                admission_id=None,
                started_at=None,
                ended_at=None,
            )
            if getattr(self, "vitals_chart", None):
                self.vitals_chart.set_visible_hours(OPERBLOCK_INITIAL_CHART_HOURS)
                if hasattr(self.vitals_chart, "set_timeline_snapshot"):
                    self.vitals_chart.set_timeline_snapshot(None, None, force=True)
                elif hasattr(self.vitals_chart, "set_operation_orders"):
                    self.vitals_chart.set_operation_orders([], None)
            self.refresh_board(force=True)
        finally:
            self._finish_opblock_action_diagnostics(action_info, "success")
            self._hide_operblock_loading(loading_key)

    def _set_protocol_chrome(self, enabled: bool):
        self.sector_8_panel.set_protocol_mode(
            enabled,
            launcher_back=bool(getattr(self, "_role_launcher_mode", False) and not enabled),
        )
        self._apply_view_only_chrome_state()

    def _current_page_requires_back_chrome(self) -> bool:
        current_widget = self.stack.currentWidget()
        return any(
            page is not None and current_widget == page
            for page in (
                self.protocol_page,
                self.archive_page,
                self.settings_page,
            )
        )

    def set_role_launcher_mode(self, enabled: bool):
        self._role_launcher_mode = bool(enabled)
        self._set_protocol_chrome(self._current_page_requires_back_chrome())

    def apply_display_settings(self):
        if hasattr(self, "sector_8_panel"):
            self.sector_8_panel.apply_display_settings()
            self._apply_view_only_chrome_state()
            self._set_protocol_chrome(self._current_page_requires_back_chrome())
        self._apply_protocol_tab_display_settings()

    def _update_protocol_current_time_label(self):
        now_dt = datetime.now()
        label = getattr(self, "protocol_current_time_label", None)
        if label is not None:
            label.setText(f"Текущее время: {now_dt.strftime('%H:%M')}")
        self._refresh_elapsed_infusion_amounts_if_needed(now_dt)

    def _refresh_elapsed_infusion_amounts_if_needed(self, now_dt: datetime):
        if self._write_pending or self.stack.currentWidget() != self.protocol_page:
            return
        snapshot = getattr(self, "_current_timeline_snapshot", None) or {}
        if not any(str((interval or {}).get("status") or "") == "active" for interval in snapshot.get("infusion_intervals") or []):
            return
        minute_key = now_dt.strftime("%Y-%m-%dT%H:%M")
        if minute_key == getattr(self, "_last_infusion_elapsed_refresh_minute", ""):
            return
        self._last_infusion_elapsed_refresh_minute = minute_key
        self._apply_active_infusions(force_elapsed=True)

    def on_back_clicked(self):
        current_widget = self.stack.currentWidget()
        if self.is_view_only_mode() and current_widget in (self.protocol_page, self.board_page, self.archive_page):
            self.view_back_requested.emit()
            return
        if current_widget == self.settings_page:
            self._on_settings_back_clicked()
            return
        if current_widget == self.archive_page:
            return_case_id = int(getattr(self, "_archive_return_operation_case_id", 0) or 0)
            self._archive_return_operation_case_id = None
            if return_case_id:
                self._open_protocol(return_case_id)
                return
            self._show_board()
            return
        if current_widget == self.protocol_page:
            if self._protocol_opened_from_archive and self.archive_page is not None:
                self._protocol_opened_from_archive = False
                self._set_protocol_chrome(True)
                self.stack.setCurrentWidget(self.archive_page)
                self.refresh_operblock_archive(force=True)
                return
            self._show_board()
            return
        if current_widget == self.board_page and getattr(self, "_role_launcher_mode", False):
            parent = self.parent()
            if parent is not None and hasattr(parent, "setCurrentIndex"):
                parent.setCurrentIndex(0)

    def _unified_controller(self):
        controller = getattr(self.window(), "unified_controller", None)
        return controller if callable(getattr(controller, "request_role_exit", None)) else None

    def _sync_roles_action_availability(self):
        panel = getattr(self, "sector_8_panel", None)
        if panel is not None:
            panel.set_roles_available(self._unified_controller() is not None)

    def _request_role_exit(self):
        controller = self._unified_controller()
        if controller is not None:
            controller.request_role_exit()

    def shutdown(self):
        self._is_closing = True
        self._close_external_archive_viewer()
        archive_page = getattr(self, "archive_page", None)
        if archive_page is not None and hasattr(archive_page, "shutdown"):
            archive_page.shutdown()
        timer = getattr(self, "_protocol_clock_timer", None)
        if timer is not None:
            timer.stop()
        worker = getattr(self, "_operation_report_pdf_worker", None)
        if worker is not None and worker.isRunning():
            worker.wait(1500)

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)

    @staticmethod
    def _clear_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            child_layout = item.layout()
            if widget:
                widget.deleteLater()
            if child_layout:
                OperBlockMainWidget._clear_layout(child_layout)
