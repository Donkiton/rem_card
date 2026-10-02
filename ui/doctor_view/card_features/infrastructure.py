from __future__ import annotations

from PySide6.QtCore import QTimer
from rem_card.app.role_session_lock import RoleSessionLock
from rem_card.app.paths import get_role_lock_path
from rem_card.app.logger import logger
import os
import socket
from .constants import ADD_PATIENT_LOCK_KEY, PATIENT_BED_MANAGEMENT_MODE

class DoctorInfrastructureMixin:
    def _preload_patient_preview_icons(self) -> None:
        if self._is_closing:
            return
        from rem_card.ui.patient_bed_management.side_patient_card import (
            preload_patient_preview_icon_pixmaps,
        )

        preload_patient_preview_icon_pixmaps()

    def _build_add_patient_lock(self) -> RoleSessionLock:
        from rem_card.app.runtime_paths import get_dev_local_operation_lock_path, is_compiled

        owner_id = f"{socket.gethostname()}:{os.getpid()}:doctor_add_patient"
        lock_path = (
            get_role_lock_path(ADD_PATIENT_LOCK_KEY)
            if is_compiled()
            else get_dev_local_operation_lock_path(ADD_PATIENT_LOCK_KEY)
        )
        data_service = getattr(getattr(self, "patient_service", None), "data_service", None)
        runtime = getattr(getattr(data_service, "db", None), "runtime_context", None)
        if getattr(runtime, "mode", "") == "emergency":
            lock_path = os.path.join(runtime.session_locks_dir, f"{ADD_PATIENT_LOCK_KEY}.lock")
        return RoleSessionLock(
            lock_path=lock_path,
            role=ADD_PATIENT_LOCK_KEY,
            owner_id=owner_id,
            owner_role="doctor",
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
                data_service.request_immediate_refresh(
                    force_emit=True,
                    source="archive_journal_exit:doctor",
                )
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
        enabled = is_beds_mode
        try:
            panel.set_add_patient_enabled(enabled)
        except RuntimeError as exc:
            logger.warning("Failed to update add-patient button state (doctor): %s", exc)
            return

        if is_beds_mode:
            self._set_add_patient_button_hint("Открыть управление пациентами")
        else:
            self._set_add_patient_button_hint("Кнопка доступна только в режиме списка коек")

    def _apply_burn_calculator_button_state(self):
        panel = getattr(self, "sector8_panel", None)
        if not self._is_qobject_alive(panel):
            return
        # Сектор 8 содержит общий «Расчёт». Доступность ожогов проверяется
        # только при открытии меню и повторно при выборе калькулятора.
        panel.set_burn_calc_enabled(False, "Доступность определяется при открытии расчётов")

    def _burn_calculator_availability(self, *, load_if_missing: bool) -> tuple[bool, str]:
        if self._selection_mode != "card" or not self.admission_id:
            return False, "Калькулятор доступен только из карты пациента"

        patient = self._burn_patient_for_context(load_if_missing=load_if_missing)
        if patient is None:
            return False, "Не удалось загрузить диагноз пациента"

        from rem_card.services.burn_infusion_calculator import is_acute_burn_mkb

        diagnosis_value = " ".join(
            filter(
                None,
                (
                    str(self._electrolyte_patient_value(patient, "mkb_code") or "").strip(),
                    str(self._electrolyte_patient_value(patient, "diagnosis_text") or "").strip(),
                ),
            )
        )
        enabled = is_acute_burn_mkb(diagnosis_value)
        tooltip = (
            "Открыть калькулятор инфузии при ожогах"
            if enabled
            else "Диагноз не относится к острым ожогам T20–T25, T27, T29–T32"
        )
        return enabled, tooltip

    def _sync_burn_patient_from_snapshot(self, snapshot: dict):
        patient = snapshot.get("patient")
        if patient is not None:
            self._burn_patient_hint = patient
        self._apply_burn_calculator_button_state()

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

            self._add_patient_locked_by_other = False
            self._apply_add_patient_button_state()
            self._apply_burn_calculator_button_state()
        except RuntimeError as exc:
            logger.warning("Failed to refresh add-patient button lock state (doctor): %s", exc)
        except Exception as exc:
            logger.warning("Unexpected add-patient button lock refresh failure (doctor): %s", exc)
