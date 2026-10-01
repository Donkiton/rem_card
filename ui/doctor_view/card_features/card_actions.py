from __future__ import annotations

from rem_card.ui.shared.custom_message_box import CustomMessageBox
from PySide6.QtWidgets import QDialog
from PySide6.QtCore import QTimer
from PySide6.QtCore import Qt
from rem_card.ui.shared.orders_balance_adapter import apply_current_order_mark_overrides
from rem_card.ui.shared.orders_balance_adapter import apply_orders_widget_mark_overrides
from datetime import datetime
from rem_card.app.logger import logger
from rem_card.app.foreground_activity import mark_foreground_activity
from rem_card.ui.shared.orders_balance_adapter import oral_totals_from_runtime
from rem_card.ui.shared.orders_balance_adapter import project_balance_orders
from rem_card.app.local_metrics import record_metric
import time
from datetime import timedelta
from .constants import CARD_HYDRATION_FOREGROUND_IDLE_SEC

class DoctorCardActionsMixin:
    def on_show_card_clicked(self):
        target_date = self._resolve_current_or_latest_card_date(self.admission_id)
        if target_date is None:
            CustomMessageBox.information(self, "Пусто", "У пациента нет сохраненных карт.")
            return
        if self._is_same_medical_day(target_date, self._current_date):
            self.refresh_data(show_empty_message=True)
            return
        self.safe_load_archived_card(
            target_date,
            balance_patient_period_manual_mode=not self._is_same_medical_day(target_date, datetime.now()),
        )

    def on_create_current_card_clicked(self):
        if not self.admission_id:
            return
        target_date = datetime.now()
        try:
            if self.service.has_card(int(self.admission_id), target_date):
                CustomMessageBox.information(self, "Создание карты", "Карта за текущие сутки уже существует.")
                return
        except Exception as exc:
            logger.warning("Failed to check current card before creation admission_id=%s: %s", self.admission_id, exc)
            CustomMessageBox.warning(
                self,
                "Создание карты",
                "Не удалось проверить наличие карты за текущие сутки. Повторите попытку после обновления данных.",
            )
            return

        if not self._is_same_medical_day(self._current_date, target_date):
            self.load_patient_card(self.admission_id, target_date, request_snapshot=False)
            if not self._is_same_medical_day(self._current_date, target_date):
                return
            self.layout_manager.set_patient_selection_mode("card")
        self.on_create_card_clicked(target_date=target_date)

    def _admission_status_is_outcome(self, admission_id: int) -> bool:
        if int(self.admission_id or 0) != int(admission_id):
            return False
        snapshot = self._card_snapshot_cache or {}
        layout = getattr(self, "layout_manager", None)
        statuses = [snapshot.get("status")]
        if getattr(layout, "_current_status_admission_id", None) == admission_id:
            statuses.append(getattr(layout, "_current_status_dto", None))
        return any(getattr(getattr(status, "status", None), "is_outcome", lambda: False)()
                   for status in statuses)

    def on_plan_card_clicked(self):
        if not self.admission_id:
            return
        self._open_or_create_plan_card(int(self.admission_id))

    def _open_or_create_plan_card(self, admission_id: int, patient=None) -> bool:
        if self._archive_read_only_mode:
            self._show_read_only_hint()
            return False
        patient_status = (getattr(patient, "_w1_runtime_snapshot", None) or {}).get("status")
        if self._admission_status_is_outcome(admission_id) or (
            getattr(getattr(patient_status, "status", None), "is_outcome", lambda: False)()
        ):
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
                "Плановая карта доступна только в последний час смены.",
            )
            return False

        target_date = state.get("plan_card_target_date")
        if target_date is None:
            target_date = self.service.get_day_period(datetime.now())[1]

        plan_exists = bool(state.get("plan_card_exists"))

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
        if not planned and self._current_status_is_outcome():
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
        patient = self.service.get_patient(self.admission_id) if not planned else None
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
        requested_at = datetime.now()

        def operation():
            if planned:
                return service.create_plan_card(admission_id, target_date, requested_at=requested_at)
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
            balance_sync = getattr(self, "_balance_snapshot_sync", None)
            if balance_sync is not None and hasattr(balance_sync, "schedule"):
                balance_sync.schedule()
            schedule_balance_update = getattr(self, "_schedule_balance_update", None)
            if callable(schedule_balance_update):
                schedule_balance_update()
            if planned:
                # Заново загружаем поступление/таймлайн после атомарного переноса.
                self.refresh_data()
            self.update_patient_info()
            message = (
                ("Плановая карта успешно создана. Вы можете заполнить её заранее."
                 if _result.get("card_created", True) else "Плановая карта открыта.")
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
        action_state_builder = getattr(self, "_sector_4v_action_state", None)
        if callable(action_state_builder):
            card_exists, _yest_exists, plan_card_available, _open_card_available = action_state_builder(snapshot)
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
        from rem_card.app.logger import logger
        from rem_card.ui.doctor_view.card_list_widget import PatientArchiveDialog
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
        from rem_card.app.logger import logger
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
            if target_id and self.service.status_service and not self._archive_read_only_mode:
                if should_ensure_initial_status:
                    logger.info(
                        "[ARCHIVE] defer initial status write to snapshot worker admission_id=%s date=%s",
                        target_id,
                        selected_date.isoformat() if hasattr(selected_date, "isoformat") else selected_date,
                    )
                else:
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
            # Не выполняем сетевую SQLite-запись в GUI-потоке. Построение
            # снимка ниже обеспечит начальный статус в фоновом потоке, если
            # открыта карта текущих медицинских суток.
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
        from rem_card.app.logger import logger
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
            from rem_card.services.balance_calculator import BalanceCalculator

            self._balance_calculator_cls = BalanceCalculator
        request_engine_reload = getattr(self, "_request_balance_engine_reload", None)
        if callable(request_engine_reload):
            request_engine_reload()
        runtime = self._balance_runtime_cache or {}
        if not runtime:
            return
        committed_orders = runtime.get("orders") or []
        orders = project_balance_orders(
            committed_orders,
            getattr(self.layout_manager, "orders_widget", None),
            self.admission_id,
            self._current_date,
        )
        orders_widget = getattr(self.layout_manager, "orders_widget", None)
        marked_orders = apply_orders_widget_mark_overrides(
            orders, orders_widget, self.admission_id, self._current_date,
        )
        if marked_orders is not None:
            orders = marked_orders
        marked_committed = apply_orders_widget_mark_overrides(
            committed_orders, orders_widget, self.admission_id, self._current_date,
        )
        if marked_committed is not None:
            committed_orders = marked_committed
        nurse_orders_widget = getattr(self.layout_manager, "nurse_orders_manager", None)
        panel_orders = apply_current_order_mark_overrides(
            orders,
            nurse_orders_widget,
            self.admission_id,
            self._current_date,
        )
        if panel_orders is not None:
            orders = panel_orders
        panel_committed_orders = apply_current_order_mark_overrides(
            committed_orders,
            nurse_orders_widget,
            self.admission_id,
            self._current_date,
        )
        if panel_committed_orders is not None:
            committed_orders = panel_committed_orders

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
            committed_orders=committed_orders,
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
        total_out_current_hour = 0
        
        if hasattr(self, 'balance_controller'): 
            total_out_cur = self.balance_controller.get_total_out_to_now()
            total_out_day = self.balance_controller.get_total_out_daily()
            total_out_current_hour = self.balance_controller.get_total_out_current_hour()
            
        sector_2b_g = getattr(self.layout_manager, 'sector_2b_g', None)
        if sector_2b_g is not None:
            sector_2b_g.update_values(
                infusion=cur["infusion"], preparats=cur["preparats"], blood=cur["blood"], plasma=cur["plasma"],
                infusion_daily=day["infusion"], preparats_daily=day["preparats"], blood_daily=day["blood"], plasma_daily=day["plasma"],
                oral=oral_cur, oral_daily=oral_day
            )
        sector_2b_v = getattr(self.layout_manager, 'sector_2b_v', None)
        if sector_2b_v is not None:
            sector_2b_v.update_balance(
                total_in_cur,
                total_out_cur,
                total_in_daily=total_in_day,
                total_out_daily=total_out_day,
                total_out_current_hour=total_out_current_hour,
            )
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
        elif tab_name == "Диета":
            widget = self._ensure_diet_widget()
            if widget is not None:
                widget.set_context(self.admission_id, self._current_date)
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
