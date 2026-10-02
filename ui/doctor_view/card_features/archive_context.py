from __future__ import annotations

from rem_card.ui.shared.async_call import AsyncCallThread
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from PySide6.QtCore import QTimer
from rem_card.services.shift_service import ShiftService
from rem_card.services.archive_readonly_service import create_archive_readonly_service
from datetime import datetime
from rem_card.app.logger import logger
import os
from rem_card.ui.shared.recovery_elapsed_time import recovery_elapsed_reference_date
from rem_card.ui.shared.recovery_elapsed_time import should_auto_update_recovery_elapsed_time

class DoctorArchiveContextMixin:
    def _set_service_context(self, service):
        self.service = service
        if hasattr(self, "layout_manager") and self.layout_manager:
            self.layout_manager.remcard_service = service
            self.layout_manager.patient_status_service = getattr(service, "status_service", None)
            if hasattr(self.layout_manager, "beds_selection_widget") and self.layout_manager.beds_selection_widget:
                self.layout_manager.beds_selection_widget.remcard_service = service
            if hasattr(self.layout_manager, "orders_widget") and self.layout_manager.orders_widget:
                self.layout_manager.orders_widget.service = service
            nurse_orders_manager = getattr(self.layout_manager, "nurse_orders_manager", None)
            if nurse_orders_manager is not None and hasattr(nurse_orders_manager, "set_service"):
                nurse_orders_manager.set_service(service)

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
        if not hasattr(self, "layout_manager") or not hasattr(self.layout_manager, "_oral_nutrition_layout"):
            return None
        from rem_card.ui.shared.components.oral_nutrition_widget import OralNutritionWidget

        self.diet_intake_widget = OralNutritionWidget(self.service, role="doctor")
        self.diet_intake_widget.data_changed.connect(self.update_balance_data)
        self.layout_manager._oral_nutrition_layout.addWidget(self.diet_intake_widget)
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
        layout = getattr(self, "layout_manager", None)
        layout_status = getattr(layout, "_current_status_dto", None)
        layout_status_owner = getattr(layout, "_current_status_admission_id", None)
        layout_status_known = bool(self.admission_id and layout_status_owner == self.admission_id)
        if layout_status_owner is not None and not layout_status_known:
            layout_status = None
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
        if self.admission_id and "status" not in snapshot and layout_status is None and not layout_status_known:
            # A fresh card context has no authoritative status until its
            # snapshot arrives.  Keep creation controls blocked instead of
            # presenting the unknown state as "no outcome".
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
            card_exists, yest_exists, plan_card_available, open_card_available = self._sector_4v_action_state(snapshot)

            # Сохраняем бизнес-логику 4в (наличие карт), добавляя только ограничение read-only.
            s4v.set_buttons_state(
                card_exists,
                yest_exists,
                plan_card_available,
                open_card_available=open_card_available,
            )
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
            current_shift_start = self._card_shift_start(datetime.now())
            if current_shift_start is None:
                return None
            non_future_dates = []
            for card_date in card_dates:
                card_shift_start = self._card_shift_start(card_date)
                if card_shift_start is not None and card_shift_start <= current_shift_start:
                    non_future_dates.append(card_date)
            if non_future_dates:
                return max(non_future_dates)
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
        same_admission = int(admission_id or 0) == int(self.admission_id or 0)
        snapshot = (self._card_snapshot_cache or {}) if same_admission else {}
        reference_dt = now or datetime.now()
        current_start = self._card_shift_start(reference_dt)
        if current_start is None:
            return {}
        _start, target_date = self.service.get_day_period(reference_dt)
        snapshot_current_start = snapshot.get("current_card_shift_start")
        current_card_known = snapshot_current_start == current_start
        snapshot_target_start = self._card_shift_start(snapshot.get("plan_card_target_date"))
        target_start = self._card_shift_start(target_date)
        plan_exists_known = snapshot_target_start == target_start
        window_active = ShiftService.is_plan_card_window(reference_dt)
        current_card_exists = bool(snapshot.get("current_card_exists")) if current_card_known else None
        return {
            "plan_card_available": bool(window_active),
            "plan_card_window_active": bool(window_active),
            "plan_card_exists": bool(snapshot.get("plan_card_exists")) if plan_exists_known else False,
            "plan_card_target_date": target_date,
            "current_card_known": current_card_known,
            "current_card_exists": current_card_exists,
        }

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
        if self._is_plan_card_open():
            # The snapshot builder performs these central reads off-thread.
            # Missing state is deliberately conservative: a new card remains
            # unavailable until an authoritative snapshot arrives.
            card_exists = bool(snapshot.get("current_card_exists", True))
            yest_exists = bool(snapshot.get("current_yest_exists", snapshot.get("yest_exists")))
            return card_exists, yest_exists, bool(snapshot.get("plan_card_available"))
        return (
            bool(snapshot.get("card_exists")),
            bool(snapshot.get("yest_exists")),
            bool(snapshot.get("plan_card_available")),
        )

    def _sector_4v_action_state(self, snapshot=None) -> tuple[bool, bool, bool, bool]:
        snapshot = snapshot if isinstance(snapshot, dict) else (self._card_snapshot_cache or {})
        selected_card_exists, yest_exists, plan_card_available = self._sector_4v_button_state(snapshot)
        current_state = self._plan_card_state_for_admission(int(self.admission_id or 0))
        current_card_exists = (
            bool(current_state.get("current_card_exists"))
            if current_state.get("current_card_known")
            else (selected_card_exists if self._is_same_medical_day(self._current_date, datetime.now()) else True)
        )
        open_card_available = bool(snapshot.get("has_any_card", selected_card_exists) or current_card_exists)
        return current_card_exists, yest_exists, plan_card_available, open_card_available

    def _resolve_current_or_latest_card_date(self, admission_id: int):
        now = datetime.now()
        try:
            if self.service.has_card(int(admission_id), now):
                return now
        except Exception as exc:
            logger.warning("Failed to resolve current card date admission_id=%s: %s", admission_id, exc)
        return self._latest_created_card_date(int(admission_id))

    def _sync_plan_card_ui_state(self):
        layout = getattr(self, "layout_manager", None)
        plan_card_open = self._is_plan_card_open()
        if layout is not None and hasattr(layout, "set_plan_card_mode"):
            layout.set_plan_card_mode(plan_card_open)
        previous = bool(getattr(self, "_last_plan_card_open_state", False))
        self._last_plan_card_open_state = bool(plan_card_open)
        current_start = self._card_shift_start(datetime.now())
        snapshot_start = (self._card_snapshot_cache or {}).get("current_card_shift_start")
        if (
            current_start is not None
            and snapshot_start != current_start
            and not self._card_state_refresh_pending
            and not self._is_closing
            and self.admission_id
        ):
            self._card_state_refresh_pending = True

            def refresh_card_button_state(expected_start=current_start):
                self._card_state_refresh_pending = False
                if self._is_closing or not self.admission_id:
                    return
                if self._card_shift_start(datetime.now()) != expected_start:
                    return
                request_snapshot = getattr(self, "_request_card_snapshot", None)
                if callable(request_snapshot):
                    request_snapshot(show_empty_message=False, load_scope="patient_open_card")

            QTimer.singleShot(0, refresh_card_button_state)
        return previous != bool(plan_card_open)

    def _balance_engine_request_is_current(self, request) -> bool:
        return bool(
            not self._is_closing
            and request is self._balance_engine_request
            and request.get("generation") == self._balance_engine_generation
        )

    def _request_balance_engine_reload(self):
        if self._is_closing or self._balance_engine_worker is not None:
            return
        calculator = self._balance_calculator_cls
        if calculator is None or not calculator.engine_reload_due():
            return
        request = {"generation": self._balance_engine_generation}
        self._balance_engine_request = request
        worker = AsyncCallThread(calculator.prepare_engine_reload_if_due)
        self._balance_engine_worker = worker
        worker.succeeded.connect(lambda prepared, req=request: self._on_balance_engine_reload_prepared(req, prepared))
        worker.failed.connect(lambda exc, req=request: self._on_balance_engine_reload_failed(req, exc))
        worker.finished.connect(lambda req=request, current=worker: self._on_balance_engine_reload_finished(req, current))
        worker.start()

    def _on_balance_engine_reload_prepared(self, request, prepared):
        if not self._balance_engine_request_is_current(request):
            return
        try:
            changed = self._balance_calculator_cls.apply_prepared_engine_reload(prepared)
        except Exception as exc:
            logger.warning("Doctor balance catalog apply failed: %s", exc)
            return
        if changed:
            self._schedule_balance_update()

    def _on_balance_engine_reload_failed(self, request, exc):
        if self._balance_engine_request_is_current(request):
            logger.warning("Doctor balance catalog refresh failed: %s", exc)

    def _on_balance_engine_reload_finished(self, request, worker):
        if worker is self._balance_engine_worker:
            self._balance_engine_worker = None
        if request is self._balance_engine_request:
            self._balance_engine_request = None

    def _should_ensure_initial_status_for_date(self, value: datetime) -> bool:
        if getattr(self, "_archive_read_only_mode", False):
            return False
        try:
            current_start, current_end = self.service.get_day_period(datetime.now())
            return current_start <= value < current_end
        except Exception as exc:
            logger.warning("Failed to resolve current medical day for initial status guard: %s", exc)
            return False

    def _update_sector_4b_patient_info(self, patient, reference_date):
        layout = getattr(self, "layout_manager", None)
        if not patient or not hasattr(layout, "sector_4b"):
            return
        auto_update = should_auto_update_recovery_elapsed_time(
            patient,
            reference_date,
            self.service,
            read_only=getattr(self, "_archive_read_only_mode", False),
        )
        display_date = recovery_elapsed_reference_date(reference_date, auto_update=auto_update)
        layout.sector_4b.update_patient_info(
            patient,
            display_date,
            auto_update_recovery_time=auto_update,
        )
