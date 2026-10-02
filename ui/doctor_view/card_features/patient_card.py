from __future__ import annotations

from rem_card.ui.shared.custom_message_box import CustomMessageBox
from PySide6.QtCore import QTimer
from rem_card.ui.shared.loading_overlay import hide_app_loading
from rem_card.app.logger import logger
from rem_card.ui.shared.loading_overlay import show_app_loading

class DoctorPatientCardMixin:
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
        if not self._ensure_full_layout(reason="patient_open"):
            if open_loading_key:
                hide_app_loading(self, open_loading_key, delay_ms=350)
            return
        self._schedule_card_ui_prewarm()
        self._ensure_card_widgets_initialized()
        from rem_card.app.logger import logger
        logger.info(f"[DOCTOR_VIEW] Loading patient card. AdmID: {admission_id}, Date: {date}")

        orders_context_unchanged = self._prepare_patient_card_orders_context(admission_id, date)
        if orders_context_unchanged is None:
            if open_loading_key:
                hide_app_loading(self, open_loading_key, delay_ms=0)
            return
        self._patient_open_generation += 1
        patient_open_generation = self._patient_open_generation
        self._balance_update_timer.stop()
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
        if not request_snapshot:
            self._initialize_provisional_balance_runtime(card_start_dt, card_end_dt)
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

    def _prepare_patient_card_orders_context(self, admission_id, date) -> bool | None:
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
            if getattr(orders_widget, "is_draft_save_pending", lambda: False)():
                CustomMessageBox.warning(
                    self,
                    "Сохранение назначений",
                    "Дождитесь завершения сохранения листа назначений перед переходом к другому пациенту.",
                )
                return None
            if orders_widget.has_drafts():
                reply = CustomMessageBox.question(
                    self,
                    "Несохраненные назначения",
                    "На текущем листе назначений есть несохраненный черновик. "
                    "Перейти к другому пациенту и отменить этот черновик?",
                    CustomMessageBox.Yes | CustomMessageBox.No,
                    CustomMessageBox.No,
                )
                if reply != CustomMessageBox.Yes:
                    return None
            orders_widget.clear_drafts()
            if getattr(orders_widget, "is_draft_save_pending", lambda: False)():
                return None
        return orders_context_unchanged

    def _reset_patient_card_context_state(self, admission_id, date, balance_patient_period_manual_mode):
        self._balance_snapshot_sync.reset()
        self.admission_id = admission_id
        self.current_date = date
        self._balance_patient_period_manual_mode = bool(balance_patient_period_manual_mode)
        self._card_snapshot_cache = None
        self._burn_patient_hint = None
        self._balance_runtime_cache = None
        self._balance_runtime_provisional = False
        try:
            card_start_dt, card_end_dt = self.service.get_day_period(date)
        except Exception:
            card_start_dt, card_end_dt = date, None
        cached_card_snapshot = self._get_cached_patient_card_snapshot(admission_id, date)
        cached_vitals_snapshot = cached_card_snapshot or self._get_cached_patient_vitals_snapshot(admission_id, date)
        if isinstance(cached_vitals_snapshot, dict):
            self._burn_patient_hint = cached_vitals_snapshot.get("patient")
        self._apply_burn_calculator_button_state()
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
        self._burn_patient_hint = patient
        self._apply_burn_calculator_button_state()
        layout = self.layout_manager
        runtime = dict(getattr(patient, "_w1_runtime_snapshot", None) or {})
        try:
            if hasattr(layout, "sector_4b"):
                self._update_sector_4b_patient_info(patient, target_date)

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
