from __future__ import annotations

from rem_card.ui.shared.custom_message_box import CustomMessageBox
from PySide6.QtCore import QTimer
from rem_card.ui.shared.window_transition import after_window_transition
from rem_card.services.archive_readonly_service import create_archive_readonly_service
from rem_card.app.logger import logger
import os
from rem_card.ui.shared.balance_snapshot_sync import show_balance_sync_status
from .constants import CARD_UI_PREWARM_STAGGER_MS, CHART_LAZY_INIT_DELAY_MS, JOURNAL_WIDGET_PREWARM_ENABLED, PATIENT_BED_MANAGEMENT_MODE

class DoctorLayoutMixin:
    def has_full_layout(self) -> bool:
        return bool(self._full_layout_created)

    def _ensure_full_layout(self, reason: str = "") -> bool:
        if self._is_closing:
            return False
        if reason == "patient_open":
            prewarmer = self._ensure_card_ui_prewarmer()
            try:
                prewarmer.finish_now(reason="patient_open")
            except Exception as exc:
                logger.warning("Doctor immediate card UI preparation failed: %s", exc, exc_info=True)
                if not self._full_layout_created:
                    return self._create_full_layout(reason=reason)
            return bool(self._full_layout_created)
        if self._full_layout_created:
            return True
        return self._create_full_layout(reason=reason)

    def _create_full_layout(self, *, reason: str, sector_preparation=None) -> bool:
        if self._is_closing:
            return False
        if self._full_layout_created:
            if sector_preparation is not None:
                sector_preparation.dispose()
            return True
        if not hasattr(self, "content_stack"):
            return False

        from rem_card.ui.shared.remcard_layout import RemCardLayoutManager

        old_shell = getattr(self, "_w1_shell", None)
        handoff = old_shell.create_layout_handoff() if old_shell is not None else None

        def create_layout(prepared_sectors=None):
            return RemCardLayoutManager(
                role="Врач",
                patient_service=self.patient_service,
                remcard_service=self.service,
                parent=self.content_stack,
                operblock_service=self.operblock_service,
                w1_handoff=handoff,
                prepared_sectors=prepared_sectors,
            )

        try:
            layout = (
                sector_preparation.build_layout(create_layout)
                if sector_preparation is not None
                else create_layout()
            )
        except Exception:
            if old_shell is not None and handoff is not None:
                old_shell.restore_layout_handoff(handoff)
            raise
        layout.patient_status_service = self.service.status_service
        layout.current_admission_id = self.admission_id
        layout.current_date = self._current_date
        layout.remcard_service = self.service
        layout.operblock_service = self.operblock_service

        if old_shell is not None and handoff is not None:
            self._disconnect_w1_shell_routing(old_shell)
            try:
                old_shell.complete_layout_handoff(handoff)
            except Exception:
                old_shell.restore_layout_handoff(handoff)
                self._connect_w1_shell_routing(old_shell)
                layout.deleteLater()
                raise

        self.content_stack.addWidget(layout)
        self.layout_manager = layout
        self._full_layout_created = True
        self._full_layout_static_signals_bound = False
        self._wire_full_layout_signals()
        self.content_stack.setCurrentWidget(layout)
        self._retire_w1_shell(old_shell)
        logger.info("[DOCTOR_VIEW] lazy full layout created reason=%s", reason)
        return True

    def _ensure_card_ui_prewarmer(self):
        prewarmer = self._card_ui_prewarmer
        if prewarmer is not None:
            return prewarmer

        from rem_card.ui.shared.staged_card_prewarm import StagedCardSectors, StagedUiPrewarm

        sector_preparation = None
        steps = []
        if not self._full_layout_created:
            sector_preparation = StagedCardSectors("doctor")
            steps.extend(sector_preparation.steps())
            steps.append(
                (
                    "layout_assembly",
                    lambda preparation=sector_preparation: self._create_full_layout(
                        reason="idle_prewarm",
                        sector_preparation=preparation,
                    ),
                )
            )
        steps.extend(
            (
                ("card_widgets", self._prewarm_card_widgets),
                ("orders_widget", self._prewarm_orders_widget),
                ("current_orders", self._prewarm_current_orders),
            )
        )
        self._card_sector_preparation = sector_preparation
        prewarmer = StagedUiPrewarm(
            self,
            role="doctor",
            steps=steps,
            stagger_ms=CARD_UI_PREWARM_STAGGER_MS,
            on_done=self._card_ui_prewarm_completed,
            on_failed=self._card_ui_prewarm_failed_callback,
            on_cancel=(sector_preparation.dispose if sector_preparation is not None else None),
        )
        self._card_ui_prewarmer = prewarmer
        return prewarmer

    def _card_ui_prewarm_completed(self):
        self._card_ui_prewarm_started = False
        self._card_ui_prewarm_done = True
        logger.debug("Doctor card UI prewarm completed")

    def _card_ui_prewarm_failed_callback(self, exc):
        self._card_ui_prewarm_started = False
        self._card_ui_prewarm_failed = True
        logger.warning("Doctor staged card UI prewarm failed: %s", exc)

    def _prewarm_card_widgets(self):
        layout = getattr(self, "layout_manager", None)
        if layout is not None:
            layout.setUpdatesEnabled(False)
        try:
            self._ensure_card_widgets_initialized()
        finally:
            if layout is not None:
                layout.setUpdatesEnabled(True)

    def _prewarm_orders_widget(self):
        ow = self._ensure_orders_widget()
        if ow is not None and getattr(ow, "main_layout", None) is None:
            ow.setup_ui()

    def _prewarm_current_orders(self):
        layout = getattr(self, "layout_manager", None)
        if layout is not None and hasattr(layout, "ensure_nurse_orders_manager"):
            layout.ensure_nurse_orders_manager()
            self._bind_nurse_orders_balance_signals()

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
            s4v.create_card_requested.connect(self.on_create_current_card_clicked)
            s4v.plan_card_requested.connect(self.on_plan_card_clicked)
            s4v.yest_card_requested.connect(self.on_yest_card_clicked)
            s4v.full_report_requested.connect(self.on_full_report_clicked)
            s4v.daily_report_requested.connect(self.on_daily_report_clicked)

        self._wire_dynamic_views()
        self._full_layout_static_signals_bound = True

    def _disconnect_w1_shell_routing(self, shell):
        if getattr(shell, "_controller_routing_disconnected", False):
            return
        try:
            if hasattr(shell, "beds_selection_widget") and shell.beds_selection_widget is not None:
                shell.beds_selection_widget.patient_selected.disconnect(self.on_patient_selected_from_list)
        except Exception:
            pass
        try:
            if hasattr(shell, "selection_mode_changed"):
                shell.selection_mode_changed.disconnect(self._on_selection_mode_changed)
        except Exception:
            pass
        shell._controller_routing_disconnected = True

    def _connect_w1_shell_routing(self, shell):
        if not getattr(shell, "_controller_routing_disconnected", False):
            return
        if getattr(shell, "beds_selection_widget", None) is not None:
            shell.beds_selection_widget.patient_selected.connect(self.on_patient_selected_from_list)
        shell.selection_mode_changed.connect(self._on_selection_mode_changed)
        shell._controller_routing_disconnected = False

    def _retire_w1_shell(self, shell):
        if shell is None:
            return
        self._disconnect_w1_shell_routing(shell)
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
        if archive_widget and archive_widget is not self._bound_archive_widget:
            archive_widget.back_requested.connect(lambda: self.on_back_clicked())
            archive_widget.patient_selected.connect(self.on_patient_selected_from_archive)
            archive_widget.edit_requested.connect(self.on_patient_edit_requested_from_archive)
            if hasattr(archive_widget, "operblock_case_selected"):
                archive_widget.operblock_case_selected.connect(self.on_operblock_case_selected_from_archive)
            self._bound_archive_widget = archive_widget
            self._archive_signals_bound = True

        admin_widget = getattr(self.layout_manager, "admin_widget", None)
        if admin_widget and admin_widget is not self._bound_admin_widget:
            self._bound_admin_widget = admin_widget
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
        if hasattr(ow, "balanceSnapshotRequired"):
            ow.balanceSnapshotRequired.connect(self._refresh_balance_from_db)
        if hasattr(ow, "committedOrdersBalanceReady"):
            ow.committedOrdersBalanceReady.connect(self._accept_committed_orders_balance_baseline)
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
        diagnostics = getattr(self, "_navigation_diagnostics", None)
        if diagnostics is not None:
            diagnostics.transition(self._selection_mode, str(mode or ""))
        self._selection_mode = str(mode or "")
        if self._selection_mode != PATIENT_BED_MANAGEMENT_MODE:
            self._release_add_patient_lock()
        self._refresh_add_patient_button_lock_state()
        self._apply_burn_calculator_button_state()

    @after_window_transition
    def _schedule_card_ui_prewarm(self):
        if self._card_ui_prewarm_started or self._card_ui_prewarm_done:
            return
        if self._card_ui_prewarm_failed:
            return
        prewarmer = self._ensure_card_ui_prewarmer()
        if prewarmer.start():
            self._card_ui_prewarm_started = True

    def _schedule_journal_prewarm(self):
        if self._journal_prewarm_started or self._journal_prewarm_done:
            return
        self._journal_prewarm_started = True
        QTimer.singleShot(0, self._run_journal_prewarm)

    @after_window_transition
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

    @after_window_transition
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

        from rem_card.ui.shared.chart_widget import ChartWidget

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
        from rem_card.ui.shared.components.balance_controller import BalanceController

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
        self.balance_controller.refresh_requested.connect(self._refresh_balance_from_db)
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
            self._refresh_balance_from_db()
        self._balance_snapshot_sync.ensure_current()

        show_balance_sync_status(self.layout_manager, self._balance_snapshot_sync.status_text)
