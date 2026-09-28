from __future__ import annotations

from rem_card.ui.shared.custom_message_box import CustomMessageBox
from PySide6.QtWidgets import QDialog
from PySide6.QtCore import QTimer
from datetime import datetime
import json
from rem_card.app.logger import logger
from .constants import PATIENT_BED_MANAGEMENT_MODE

class DoctorNavigationMixin:
    def on_exit_clicked(self):
        reply = CustomMessageBox.question(self, "Подтверждение", "Выйти из программы?", CustomMessageBox.Yes | CustomMessageBox.No, CustomMessageBox.No)
        if reply == CustomMessageBox.Yes:
            controller = getattr(self.window(), "unified_controller", None)
            if controller is not None:
                controller.request_application_exit(confirmed=True)
            else:
                self.window().close()

    def on_back_clicked(self):
        diagnostics = getattr(self, "_navigation_diagnostics", None)
        if diagnostics is not None:
            diagnostics.action("back_requested")
        viewer = getattr(self, "_operblock_archive_viewer", None)
        if (
            hasattr(self, "content_stack")
            and self._is_qobject_alive(viewer)
            and self.content_stack.currentWidget() == viewer
        ):
            self._return_from_operblock_archive_viewer()
            return
        current_idx = self.layout_manager.selection_stack.currentIndex()
        journal_idx = -1
        if hasattr(self.layout_manager, "journal_view"):
            journal_idx = self.layout_manager.selection_stack.indexOf(self.layout_manager.journal_view)
        was_journal_mode = (current_idx == journal_idx and journal_idx != -1)
        admin_idx = -1
        if hasattr(self.layout_manager, "admin_view"):
            admin_idx = self.layout_manager.selection_stack.indexOf(self.layout_manager.admin_view)

        if current_idx == 0 and not self._archive_read_only_mode:
            orders_widget = getattr(self.layout_manager, "orders_widget", None)
            if orders_widget is not None:
                if getattr(orders_widget, "is_draft_save_pending", lambda: False)():
                    CustomMessageBox.warning(
                        self,
                        "Сохранение назначений",
                        "Дождитесь завершения сохранения листа назначений перед выходом из карты.",
                    )
                    return
                if orders_widget.has_drafts():
                    reply = CustomMessageBox.question(
                        self,
                        "Несохранённые назначения",
                        "В листе назначений есть несохранённый черновик. Выйти из карты и отменить его?",
                        CustomMessageBox.Yes | CustomMessageBox.No,
                        CustomMessageBox.No,
                    )
                    if reply != CustomMessageBox.Yes:
                        return
                    orders_widget.clear_drafts()
                    if getattr(orders_widget, "is_draft_save_pending", lambda: False)():
                        return

        self._balance_update_timer.stop()

        if current_idx == admin_idx and admin_idx != -1:
            admin_widget = getattr(self.layout_manager, "admin_widget", None)
            if admin_widget is not None and hasattr(admin_widget, "go_back") and admin_widget.go_back():
                return
            if self._return_from_settings():
                return

        if current_idx == 0 and self._card_return_mode == "archive":
            self.admission_id = None
            self._release_add_patient_lock()
            self._exit_archive_read_only_mode()
            self._card_return_mode = None
            self._card_opened_from_global_archive = False
            self.layout_manager.set_patient_selection_mode("archive")
            self._wire_dynamic_views()
            self.layout_manager.bottom_row.hide()
        elif current_idx == 0:
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

    def _unified_controller(self):
        controller = getattr(self.window(), "unified_controller", None)
        return controller if callable(getattr(controller, "request_role_exit", None)) else None

    def _sync_roles_action_availability(self):
        panel = getattr(self, "sector8_panel", None)
        if panel is not None:
            panel.set_roles_available(self._unified_controller() is not None)

    def _request_role_exit(self):
        controller = self._unified_controller()
        if controller is not None:
            controller.request_role_exit()

    def on_settings_clicked(self):
        self._remember_settings_return_mode()
        self._exit_archive_read_only_mode()
        self.layout_manager.set_patient_selection_mode("admin")
        self._wire_dynamic_views()
        self.layout_manager.bottom_row.hide()
        admin_widget = getattr(self.layout_manager, "admin_widget", None)
        if admin_widget:
            admin_widget.set_print_context(self.service, self.admission_id, self._current_date)

    def on_user_report_clicked(self):
        from rem_card.ui.shared.user_reports_dialog import UserReportDialog

        dialog = UserReportDialog(role="doctor", parent=self)
        dialog.submitted.connect(self._refresh_user_reports_count)
        dialog.exec()
        self._refresh_user_reports_count()

    def on_user_reports_clicked(self):
        from rem_card.ui.shared.user_reports_dialog import UserReportsInboxDialog

        dialog = UserReportsInboxDialog(role="doctor", parent=self)
        dialog.reports_changed.connect(self._refresh_user_reports_count)
        dialog.exec()
        self._refresh_user_reports_count()

    def _refresh_user_reports_count(self):
        panel = getattr(self, "sector8_panel", None)
        method = getattr(panel, "refresh_user_reports_count", None)
        if callable(method):
            method()

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
            holder_role = self._add_patient_lock.holder_owner_role()
            holder_label = {"doctor": "врача", "nurse": "медсестры"}.get(holder_role)
            message = "Окно добавления пациента уже открыто.\nПожалуйста, подождите."
            if holder_label:
                message = f"Окно добавления пациента уже открыто у {holder_label}.\nПожалуйста, подождите."
            CustomMessageBox.warning(
                self,
                "Добавление занято",
                message,
            )
            self._refresh_add_patient_button_lock_state()
            return

        try:
            self.layout_manager.set_patient_selection_mode(PATIENT_BED_MANAGEMENT_MODE)
            self.layout_manager.bottom_row.hide()
        except Exception:
            self._release_add_patient_lock()
            raise

    def on_refresh_beds_clicked(self):
        self.force_refresh_everywhere()
        if hasattr(self, 'chart'):
            self.chart.update()

    def force_refresh_everywhere(self):
        """Принудительно обновляет максимум доступных представлений без изменения UI-структуры."""
        data_service = self._get_data_service()
        if data_service:
            data_service.request_immediate_refresh(
                force_emit=True,
                source="manual_refresh:doctor",
            )

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

    def on_calculations_clicked(self):
        from rem_card.ui.shared.components.calculation_launcher import (
            CALCULATION_BURNS,
            CALCULATION_ELECTROLYTES,
            CALCULATION_INFUSION,
            run_calculation_launcher,
        )

        burn_enabled, burn_reason = self._burn_calculator_availability(load_if_missing=True)
        self.sector8_panel.set_burn_calc_enabled(burn_enabled, burn_reason)
        calculation, anchor_center = run_calculation_launcher(
            self,
            burn_enabled=burn_enabled,
            burn_disabled_reason=burn_reason,
        )
        handler = {
            CALCULATION_INFUSION: self.on_calculator_clicked,
            CALCULATION_ELECTROLYTES: self.on_electrolyte_calculator_clicked,
            CALCULATION_BURNS: self.on_burn_calculator_clicked,
        }.get(calculation)
        if handler is not None:
            # Возвращаем управление основному event loop, чтобы Qt успел
            # полностью закрыть launcher перед созданием следующего native окна.
            QTimer.singleShot(
                0,
                lambda callback=handler, center=anchor_center: callback(anchor_center=center),
            )

    def on_calculator_clicked(self, *, anchor_center=None):
        from rem_card.ui.shared.components.infusion_calculator import InfusionCalculatorDialog
        from rem_card.ui.shared.components.calculation_launcher import exec_calculation_dialog

        # Чистый запуск без передачи веса пациента (калькулятор стартует с 0)
        dialog = InfusionCalculatorDialog(parent=self)
        # Этот калькулятор сам восстанавливает сохранённую позицию окна.
        exec_calculation_dialog(dialog)

    def on_burn_calculator_clicked(self, *, anchor_center=None):
        from rem_card.services.burn_infusion_calculator import is_acute_burn_mkb
        from rem_card.ui.shared.components.burn_infusion_calculator import BurnInfusionCalculatorDialog
        from rem_card.ui.shared.components.calculation_launcher import exec_calculation_dialog

        patient = self._burn_patient_for_context(load_if_missing=True)
        if patient is None:
            CustomMessageBox.information(self, "Калькулятор ожогов", "Не удалось загрузить данные пациента.")
            return
        diagnosis_value = " ".join(
            filter(
                None,
                (
                    str(self._electrolyte_patient_value(patient, "mkb_code") or "").strip(),
                    str(self._electrolyte_patient_value(patient, "diagnosis_text") or "").strip(),
                ),
            )
        )
        if self._selection_mode != "card" or not is_acute_burn_mkb(diagnosis_value):
            CustomMessageBox.information(
                self,
                "Калькулятор ожогов",
                "Калькулятор доступен из карты пациента при диагнозе T20–T25, T27 или T29–T32.",
            )
            self._apply_burn_calculator_button_state()
            return

        dialog = BurnInfusionCalculatorDialog(
            parent=self,
            patient_context=self._build_burn_calculator_context(patient),
        )
        exec_calculation_dialog(dialog, anchor_center)

    def _burn_patient_for_context(self, *, load_if_missing: bool):
        snapshot = self._card_snapshot_cache or {}
        patient = snapshot.get("patient") or self._burn_patient_hint
        if patient is None and load_if_missing and self.admission_id and hasattr(self.service, "get_patient"):
            try:
                patient = self.service.get_patient(int(self.admission_id))
            except Exception as exc:
                logger.warning("Burn calculator: failed to load patient context: %s", exc)
        if patient is not None:
            self._burn_patient_hint = patient
        return patient

    def _build_burn_calculator_context(self, patient) -> dict:
        from rem_card.ui.shared.patient_calculator_context import build_burn_context

        return build_burn_context(
            self.service, int(self.admission_id), patient, shift_date=self._current_date
        )

    def on_electrolyte_calculator_clicked(self, *, anchor_center=None):
        from rem_card.ui.shared.components.electrolyte_calculator import ElectrolyteCalculatorDialog
        from rem_card.ui.shared.components.calculation_launcher import exec_calculation_dialog

        dialog = ElectrolyteCalculatorDialog(
            parent=self,
            patient_context=self._build_electrolyte_calculator_context(),
        )
        exec_calculation_dialog(dialog, anchor_center)

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
        from ..doctor_remcard_widget import DoctorRemCardWidget
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
                data_service.request_immediate_refresh(
                    force_emit=True,
                    source="archive_patient_edit:doctor",
                )
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
            target_date = self._resolve_current_or_latest_card_date(patient.id)
            if target_date is None:
                CustomMessageBox.information(self, "Пусто", "У пациента нет сохраненных карт.")
                return
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
            from rem_card.app.logger import logger
            logger.error(f"Error updating latest indicators: {e}")

    def update_patient_info(self):
        try:
            snapshot = self._card_snapshot_cache or {}
            patient = snapshot.get("patient")
            self._sync_burn_patient_from_snapshot(snapshot)
            if patient and hasattr(self.layout_manager, 'sector_4b'):
                self._update_sector_4b_patient_info(patient, self._current_date)
            if hasattr(self.layout_manager, 'sector_4v'):
                card_exists, yest_exists, plan_card_available, open_card_available = self._sector_4v_action_state(snapshot)
                self.layout_manager.sector_4v.set_buttons_state(
                    card_exists,
                    yest_exists,
                    plan_card_available,
                    open_card_available=open_card_available,
                )
                self.update_latest_indicators()
                self._apply_archive_read_only_state()
            if hasattr(self.layout_manager, "set_current_status_dto"):
                self.layout_manager.set_current_status_dto(snapshot.get("status"))
            self.layout_manager.refresh_current_status()
        except Exception as e:
            from rem_card.app.logger import logger
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
