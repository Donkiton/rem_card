from __future__ import annotations

from PySide6.QtWidgets import QDialog
from PySide6.QtWidgets import QPushButton
from rem_card.services.concurrency import DataConflictError
from rem_card.services.operblock_medication_presets import build_operblock_preset_bolus_text
from rem_card.services.operblock_medication_presets import build_operblock_preset_payload
from rem_card.services.operblock_medication_presets import normalize_operblock_medication_preset_kind
from rem_card.services.operblock_medication_presets import operblock_medication_preset_display_name
from rem_card.services.operblock_quick_orders import build_operblock_quick_order_text
from rem_card.services.operblock_quick_orders import normalize_operblock_quick_order_kind
from rem_card.services.operblock_service import OperBlockConflictError
from rem_card.ui.shared.custom_message_box import CustomMessageBox
import re
import time
from rem_card.ui.operblock_view.operblock_helpers import (
    _build_order_text_for_display,
    _gas_display_name_for_payload,
    _gas_identity_matches,
    _infusion_display_drug_name,
    _minute_floor_dt,
    _normalize_bolus_dose_text,
    _normalize_gas_dose_text,
    _normalize_oxygen_flow_text,
    _normalize_volume_ml_text,
    _order_route_code,
    _oxygen_payload_fields,
    _parse_datetime_value,
    _payload_or_text_is_oxygen,
    _quick_order_dose_display_text,
    _quick_order_dose_volume_ml,
    _quick_order_mass_dose_component,
    _safe_int,
    _source_solvent_volume_ml,
    _split_infusion_rate_text,
    _split_order_drug_and_dose,
    _timed_infusion_dose_options,
    _timed_infusion_total_volume_ml,
    _volume_text_without_unit,
)
from rem_card.ui.operblock_view.operblock_medication_edit_dialogs import (
    BolusEditDialog,
    TimeEditDialog,
)
from rem_card.ui.operblock_view.operblock_visual_primitives import (
    OPERBLOCK_ADD_ORDER_BUTTON_TEXT,
)


class OperBlockOrderActionsMixin:
    @staticmethod
    def _manual_single_order_payload(kind: str, drug_text: str) -> dict[str, str | bool]:
        clean_kind = normalize_operblock_medication_preset_kind(kind)
        clean_label = re.sub(r"\s+", " ", str(drug_text or "").strip())
        slug = re.sub(r"[^0-9A-Za-zА-Яа-яЁё]+", "_", clean_label.casefold()).strip("_") or "order"
        return {
            "preset_id": f"manual:{clean_kind}:{slug}",
            "kind": clean_kind,
            "label": clean_label,
            "display_name": clean_label,
            "manual_order": True,
        }

    def _save_order(self):
        if self.is_view_only_mode():
            return
        if not self._current_admission_id or self._write_pending:
            return
        if not self._orders_tab_enabled():
            CustomMessageBox.warning(self, "Пособие не начато", "Назначения доступны только во время анестезиологического пособия.")
            self.refresh_protocol(force=True)
            return
        drug_text = self.order_input.text().strip()
        dose_text = self.order_dose_input.text().strip() if hasattr(self, "order_dose_input") else ""
        rate_text = self.order_rate_input.text().strip() if hasattr(self, "order_rate_input") else ""
        kind = self._manual_order_kind()
        if kind == "continuous_infusion":
            self._start_manual_continuous_infusion(drug_text, dose_text, rate_text)
            return
        if kind == "timed_infusion":
            self._start_manual_timed_infusion(drug_text, dose_text)
            return
        if kind == "gas":
            self._start_manual_gas(drug_text, dose_text)
            return

        text = drug_text
        if text and dose_text and dose_text.casefold() not in text.casefold():
            text = f"{text} {dose_text}".strip()
        if not text:
            CustomMessageBox.warning(self, "Ошибка", "Текст назначения не заполнен.")
            return
        self._write_pending = True
        self._set_protocol_write_controls_enabled(False)
        self.save_order_button.setText("Сохранение...")
        preset_payload = self._manual_single_order_payload(kind, drug_text) if kind == "gas" else None

        def operation():
            return self.operblock_service.add_order(
                self._current_admission_id,
                text,
                preset_payload=preset_payload,
                return_row=True,
            )

        write_description = f"operblock_add_{kind}_order:{self._current_admission_id}"
        self._remember_local_write_refresh_suppression(write_description, {"orders"})
        self._enqueue_write(
            write_description,
            operation,
            on_success=lambda result: self._on_order_saved(result),
            on_error=lambda exc: self._on_protocol_write_error(exc, self.save_order_button, OPERBLOCK_ADD_ORDER_BUTTON_TEXT),
        )

    def _on_order_saved(self, result=None):
        self._write_pending = False
        self._set_protocol_write_controls_enabled(True)
        self._clear_manual_order_inputs()
        if not self._apply_added_order_locally(dict(result or {}) if isinstance(result, dict) else {}):
            self.refresh_protocol(force=True)

    def _clear_manual_order_inputs(self):
        if hasattr(self, "order_input"):
            self.order_input.clear()
        if hasattr(self, "order_dose_input"):
            self.order_dose_input.clear()
        if hasattr(self, "order_rate_input"):
            self.order_rate_input.clear()

    def _on_manual_infusion_saved(self, result=None):
        self._clear_manual_order_inputs()
        self._on_infusion_mutation_saved(result)

    def _manual_infusion_payload(self, kind: str, dose_text: str, volume: str = "") -> dict[str, str | bool]:
        payload: dict[str, str | bool] = {"kind": kind, "manual_order": True}
        clean_dose = re.sub(r"\s+", " ", str(dose_text or "").strip())
        if clean_dose:
            payload["dose_text"] = clean_dose
            payload["display_dose_text"] = clean_dose
        if volume:
            payload["volume_ml"] = volume
            payload["declared_total_volume_ml"] = volume
        return payload

    def _start_manual_continuous_infusion(self, drug_text: str, dose_text: str, rate_text: str):
        if self.is_view_only_mode():
            return
        drug_name = re.sub(r"\s+", " ", str(drug_text or "").strip())
        clean_dose = re.sub(r"\s+", " ", str(dose_text or "").strip())
        if not drug_name:
            CustomMessageBox.warning(self, "Дозатор", "Укажите препарат для дозатора.")
            return
        rate_value, rate_unit = _split_infusion_rate_text(rate_text)
        if not rate_value or not rate_unit:
            CustomMessageBox.warning(self, "Дозатор", "Укажите скорость в мл/час, например: 1 мл/час.")
            return
        if clean_dose:
            drug_name = build_operblock_quick_order_text(drug_name, clean_dose)
        event_time = self._current_operation_event_time_text()
        if not self._validate_infusion_event_datetime_or_warn(event_time):
            return
        quick_scroll_state = self._remember_quick_orders_scroll_state()
        self._write_pending = True
        self._set_protocol_write_controls_enabled(False)
        self._restore_quick_orders_scroll_state_later(quick_scroll_state)
        payload = self._manual_infusion_payload("continuous_infusion", clean_dose)

        def operation():
            return self.operblock_service.start_infusion(
                self._current_admission_id,
                self._current_operation_case_id,
                drug_name,
                rate_value,
                rate_unit,
                event_time,
                payload=payload,
                return_event=True,
            )

        write_description = f"operblock_manual_dozator:{self._current_admission_id}"
        self._remember_local_write_refresh_suppression(write_description, {"operblock_timeline_events"})
        self._enqueue_write(
            write_description,
            operation,
            on_success=lambda result: self._on_manual_infusion_saved(result),
            on_error=lambda exc: self._on_infusion_mutation_error(exc),
        )

    def _start_manual_timed_infusion(self, drug_text: str, dose_text: str):
        if self.is_view_only_mode():
            return
        drug_name = re.sub(r"\s+", " ", str(drug_text or "").strip())
        clean_dose = re.sub(r"\s+", " ", str(dose_text or "").strip())
        if not drug_name:
            CustomMessageBox.warning(self, "Капельница", "Укажите препарат для капельницы.")
            return
        if not clean_dose:
            CustomMessageBox.warning(self, "Капельница", "Укажите объем или дозу капельницы.")
            return
        volume = _normalize_volume_ml_text(clean_dose)
        event_time = self._current_operation_event_time_text()
        if not self._validate_infusion_event_datetime_or_warn(event_time):
            return
        quick_scroll_state = self._remember_quick_orders_scroll_state()
        self._write_pending = True
        self._set_protocol_write_controls_enabled(False)
        self._restore_quick_orders_scroll_state_later(quick_scroll_state)
        payload = self._manual_infusion_payload("timed_infusion", clean_dose, volume)
        infusion_drug_name = build_operblock_quick_order_text(drug_name, clean_dose) if clean_dose and not volume else drug_name

        def operation():
            return self.operblock_service.start_infusion(
                self._current_admission_id,
                self._current_operation_case_id,
                infusion_drug_name,
                None,
                "",
                event_time,
                volume_ml=volume,
                payload=payload,
                return_event=True,
            )

        write_description = f"operblock_manual_timed_infusion:{self._current_admission_id}"
        self._remember_local_write_refresh_suppression(write_description, {"operblock_timeline_events"})
        self._enqueue_write(
            write_description,
            operation,
            on_success=lambda result: self._on_manual_infusion_saved(result),
            on_error=lambda exc: self._on_infusion_mutation_error(exc),
        )

    def _start_gas_infusion(
        self,
        drug_text: str,
        dose_text: str,
        *,
        payload: dict | None = None,
        source_key: str = "operblock_gas",
        on_saved=None,
    ):
        if self.is_view_only_mode():
            return
        if not self._current_admission_id or self._write_pending:
            return
        if not self._ensure_infusion_write_context_or_warn():
            return
        drug_name = re.sub(r"\s+", " ", str(drug_text or "").strip())
        if not drug_name:
            CustomMessageBox.warning(self, "Газ", "Укажите газ.")
            return
        effective_payload = dict(payload or {}) if isinstance(payload, dict) else {}
        is_oxygen = _payload_or_text_is_oxygen(effective_payload, drug_name)
        clean_dose = _normalize_oxygen_flow_text(dose_text) if is_oxygen else _normalize_gas_dose_text(dose_text)
        if not clean_dose:
            if is_oxygen:
                CustomMessageBox.warning(self, "Кислород", "Укажите поток кислорода, например: 10 л/мин.")
            else:
                CustomMessageBox.warning(self, "Газ", "Укажите дозу газа, например: 0,7 MAC.")
            return
        success_callback = on_saved or self._on_infusion_mutation_saved
        if is_oxygen:
            effective_payload = _oxygen_payload_fields(effective_payload, clean_dose)
        effective_payload["kind"] = "gas"
        effective_payload["dose_text"] = clean_dose
        effective_payload["display_dose_text"] = clean_dose
        effective_payload.setdefault("label", drug_name)
        effective_payload.setdefault("display_name", drug_name)
        active_gas = self._active_gas_interval(oxygen=is_oxygen)
        if active_gas is not None:
            if not _gas_identity_matches(active_gas, drug_name, effective_payload):
                active_name = _infusion_display_drug_name(active_gas, "Газ")
                requested_name = _gas_display_name_for_payload(drug_name, effective_payload)
                CustomMessageBox.warning(
                    self,
                    "Газ уже идет",
                    (
                        f"Сейчас активен газ: {active_name}.\n"
                        "Одновременное использование двух разных газов запрещено. "
                        f"Сначала остановите активный газ, затем назначьте {requested_name}."
                    ),
                )
                return
            self._update_gas_dose_direct(
                active_gas,
                clean_dose,
                source_key=f"{source_key}_active",
                on_saved=success_callback if on_saved is not None else None,
            )
            return
        event_time = self._current_operation_event_time_text()
        if not self._validate_infusion_event_datetime_or_warn(event_time):
            return
        quick_scroll_state = self._remember_quick_orders_scroll_state()
        self._write_pending = True
        self._set_protocol_write_controls_enabled(False)
        self._restore_quick_orders_scroll_state_later(quick_scroll_state)

        def operation():
            return self.operblock_service.start_infusion(
                self._current_admission_id,
                self._current_operation_case_id,
                drug_name,
                None,
                "",
                event_time,
                payload=effective_payload,
                return_event=True,
            )

        write_description = f"{source_key}:{self._current_admission_id}"
        self._remember_local_write_refresh_suppression(write_description, {"operblock_timeline_events"})
        self._enqueue_write(
            write_description,
            operation,
            on_success=lambda result: success_callback(result),
            on_error=lambda exc: self._on_infusion_mutation_error(exc),
        )

    def _start_manual_gas(self, drug_text: str, dose_text: str):
        self._start_gas_infusion(
            drug_text,
            dose_text,
            payload={"manual_order": True},
            source_key="operblock_manual_gas",
            on_saved=self._on_manual_infusion_saved,
        )

    def _on_protocol_write_error(self, exc: Exception, button: QPushButton, label: str):
        self._write_pending = False
        self._set_protocol_write_controls_enabled(True)
        button.setText(label)
        title = "Конфликт данных" if isinstance(exc, (DataConflictError, OperBlockConflictError)) else "Ошибка сохранения"
        CustomMessageBox.warning(self, title, str(exc))
        self.refresh_protocol(force=True)

    def _validate_order_datetime_or_warn(self, value: str) -> bool:
        order_dt = _minute_floor_dt(_parse_datetime_value(value))
        if order_dt is None:
            CustomMessageBox.warning(self, "Время назначения", "Укажите корректное время введения препарата.")
            return False
        start_dt = _minute_floor_dt(self._current_anesthesia_start)
        if start_dt and order_dt < start_dt:
            CustomMessageBox.warning(
                self,
                "Время назначения",
                f"Назначение не может быть раньше начала пособия: {start_dt.strftime('%d.%m.%Y %H:%M')}.",
            )
            return False
        end_dt = _minute_floor_dt(self._current_anesthesia_end) if not self._current_anesthesia_active else None
        if end_dt and order_dt > end_dt:
            CustomMessageBox.warning(
                self,
                "Время назначения",
                f"Назначение не может быть позже окончания пособия: {end_dt.strftime('%d.%m.%Y %H:%M')}.",
            )
            return False
        return True

    def _validate_infusion_event_datetime_or_warn(self, value: str | None) -> bool:
        event_dt = _minute_floor_dt(_parse_datetime_value(value))
        if event_dt is None:
            CustomMessageBox.warning(self, "Время назначения", "Укажите корректное время начала назначения.")
            return False
        start_dt = _minute_floor_dt(self._current_anesthesia_start)
        if start_dt and event_dt < start_dt:
            CustomMessageBox.warning(
                self,
                "Время назначения",
                f"Старт назначения не может быть раньше начала пособия: {start_dt.strftime('%d.%m.%Y %H:%M')}.",
            )
            return False
        end_dt = _minute_floor_dt(self._current_anesthesia_end) if not self._current_anesthesia_active else None
        if end_dt and event_dt > end_dt:
            CustomMessageBox.warning(
                self,
                "Время назначения",
                f"Старт назначения не может быть позже окончания пособия: {end_dt.strftime('%d.%m.%Y %H:%M')}.",
            )
            return False
        return True

    def _validate_infusion_stop_datetime_or_warn(self, value: str | None, interval: dict) -> bool:
        event_dt = _minute_floor_dt(_parse_datetime_value(value))
        if event_dt is None:
            CustomMessageBox.warning(self, "Время остановки", "Укажите корректное время окончания назначения.")
            return False
        start_dt = _minute_floor_dt(_parse_datetime_value((interval or {}).get("start_time")))
        if start_dt and event_dt < start_dt:
            CustomMessageBox.warning(
                self,
                "Время остановки",
                f"Остановка назначения не может быть раньше старта: {start_dt.strftime('%d.%m.%Y %H:%M')}.",
            )
            return False
        latest_dt = self._latest_infusion_event_datetime(interval or {})
        if latest_dt and event_dt < latest_dt:
            CustomMessageBox.warning(
                self,
                "Время остановки",
                f"Остановка назначения не может быть раньше последнего изменения: {latest_dt.strftime('%d.%m.%Y %H:%M')}.",
            )
            return False
        anesthesia_start_dt = _minute_floor_dt(self._current_anesthesia_start)
        if anesthesia_start_dt and event_dt < anesthesia_start_dt:
            CustomMessageBox.warning(
                self,
                "Время остановки",
                f"Остановка назначения не может быть раньше начала пособия: {anesthesia_start_dt.strftime('%d.%m.%Y %H:%M')}.",
            )
            return False
        anesthesia_end_dt = _minute_floor_dt(self._current_anesthesia_end) if not self._current_anesthesia_active else None
        if anesthesia_end_dt and event_dt > anesthesia_end_dt:
            CustomMessageBox.warning(
                self,
                "Время остановки",
                f"Остановка назначения не может быть позже окончания пособия: {anesthesia_end_dt.strftime('%d.%m.%Y %H:%M')}.",
            )
            return False
        return True

    @staticmethod
    def _route_only_write_description(admission_id: int, order_id: int) -> str:
        return f"operblock_update_order_route:{int(admission_id)}:{int(order_id)}"

    def _remember_route_only_write(self, admission_id: int, order_id: int) -> None:
        self._route_only_write_suppressions[(int(admission_id), int(order_id))] = time.monotonic()

    @staticmethod
    def _bolus_order_dialog_parts(row: dict) -> tuple[str, str]:
        clean_text = re.sub(r"\s+", " ", str((row or {}).get("text") or "").strip())
        drug_name = re.sub(
            r"\s+",
            " ",
            str((row or {}).get("drug_name") or (row or {}).get("drug_label") or "").strip(),
        )
        dose_text = _normalize_bolus_dose_text(str((row or {}).get("dose_text") or "").strip())
        if drug_name and dose_text:
            return drug_name, dose_text
        if drug_name and clean_text.casefold().startswith(drug_name.casefold()):
            tail = clean_text[len(drug_name) :].strip(" -:;·")
            if tail and not dose_text:
                dose_text = _normalize_bolus_dose_text(tail)
            return drug_name, dose_text
        parsed_drug, parsed_dose = _split_order_drug_and_dose(clean_text)
        if parsed_dose:
            if not dose_text:
                dose_text = _normalize_bolus_dose_text(parsed_dose)
            if not drug_name:
                drug_name = parsed_drug
        elif not dose_text:
            trailing_number = re.search(r"(?P<dose>\d+(?:[.,]\d+)?)$", clean_text)
            if trailing_number:
                prefix = clean_text[: trailing_number.start()].strip()
                if prefix:
                    dose_text = _normalize_bolus_dose_text(trailing_number.group("dose"))
                    if not drug_name:
                        drug_name = prefix
        if not drug_name:
            drug_name = clean_text
        return drug_name or "Препарат", dose_text

    def _edit_order_with_time(self, row: dict):
        if self.is_view_only_mode():
            return
        if not self._current_admission_id or self._write_pending:
            return
        order_id = _safe_int(row.get("id"))
        if not order_id:
            return
        row = self._fresh_order_row(row)
        old_datetime = _minute_floor_dt(_parse_datetime_value(row.get("datetime")))
        if old_datetime is None:
            CustomMessageBox.warning(self, "Время назначения", "Не удалось определить время назначения. Обновите протокол.")
            self.refresh_protocol(force=True)
            return
        old_text = str(row.get("text") or "").strip()
        if not old_text:
            CustomMessageBox.warning(self, "Ошибка", "Текст назначения не заполнен.")
            return
        old_route = _order_route_code(row)
        drug_name, dose_text = self._bolus_order_dialog_parts(row)
        route_options = self._route_options_for_order_row(row)
        dialog = BolusEditDialog(
            drug_name,
            dose_text,
            self,
            base_datetime=old_datetime,
            min_datetime=self._current_anesthesia_start or self._current_operation_start,
            max_datetime=self._current_anesthesia_end,
            route_code=old_route,
            route_options=route_options,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        text = dialog.text()
        if not text:
            CustomMessageBox.warning(self, "Ошибка", "Текст назначения не заполнен.")
            return
        order_datetime = dialog.datetime_text()
        if not self._validate_order_datetime_or_warn(order_datetime):
            self.refresh_protocol(force=True)
            return
        new_datetime = _minute_floor_dt(_parse_datetime_value(order_datetime))
        route_code = dialog.route_code()
        if text == old_text and new_datetime == old_datetime and route_code == old_route:
            return
        route_only = text == old_text and new_datetime == old_datetime and route_code != old_route
        expected_revision = None if route_only else int(row.get("revision") or 0)
        self._write_pending = True
        if not route_only:
            self._set_protocol_write_controls_enabled(False)
        if route_only:
            self._remember_route_only_write(self._current_admission_id, order_id)

        def operation():
            return self.operblock_service.update_order_text(
                self._current_admission_id,
                order_id,
                text,
                None if route_only else order_datetime,
                expected_revision=expected_revision,
                route=route_code,
            )

        write_description = (
            self._route_only_write_description(self._current_admission_id, order_id)
            if route_only
            else f"operblock_update_order:{self._current_admission_id}:{order_id}"
        )
        if not route_only:
            self._remember_local_write_refresh_suppression(write_description, {"orders"})
        on_success = (
            (lambda _result, oid=order_id, route=route_code: self._on_order_route_saved(oid, route))
            if route_only
            else (
                lambda _result, oid=order_id, new_text=text, dt=order_datetime, route=route_code: self._on_order_edit_saved(
                    oid,
                    new_text,
                    order_datetime=dt,
                    route_code=route,
                )
            )
        )
        self._enqueue_write(
            write_description,
            operation,
            on_success=on_success,
            on_error=lambda exc: self._on_order_mutation_error(exc),
        )

    def _edit_order(self, row: dict):
        if not self._current_admission_id or self._write_pending:
            return
        order_id = _safe_int(row.get("id"))
        if not order_id:
            return
        row = self._fresh_order_row(row)
        old_route = _order_route_code(row)
        old_text = str(row.get("text") or "").strip()
        drug_name, dose_text = self._bolus_order_dialog_parts(row)
        route_options = self._route_options_for_order_row(row)
        dialog = BolusEditDialog(
            drug_name,
            dose_text,
            self,
            route_code=old_route,
            route_options=route_options,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        text = dialog.text()
        if not text:
            CustomMessageBox.warning(self, "Ошибка", "Текст назначения не заполнен.")
            return
        route_code = dialog.route_code()
        if text == old_text and route_code == old_route:
            return
        route_only = text == old_text and route_code != old_route
        expected_revision = None if route_only else int(row.get("revision") or 0)
        self._write_pending = True
        if not route_only:
            self._set_protocol_write_controls_enabled(False)
        if route_only:
            self._remember_route_only_write(self._current_admission_id, order_id)

        def operation():
            return self.operblock_service.update_order_text(
                self._current_admission_id,
                order_id,
                text,
                None,
                expected_revision=expected_revision,
                route=route_code,
            )

        write_description = (
            self._route_only_write_description(self._current_admission_id, order_id)
            if route_only
            else f"operblock_update_order:{self._current_admission_id}:{order_id}"
        )
        if not route_only:
            self._remember_local_write_refresh_suppression(write_description, {"orders"})
        on_success = (
            (lambda _result, oid=order_id, route=route_code: self._on_order_route_saved(oid, route))
            if route_only
            else (
                lambda _result, oid=order_id, new_text=text, route=route_code: self._on_order_edit_saved(
                    oid,
                    new_text,
                    route_code=route,
                )
            )
        )
        self._enqueue_write(
            write_description,
            operation,
            on_success=on_success,
            on_error=lambda exc: self._on_order_mutation_error(exc),
        )

    def _edit_order_time(self, row: dict):
        if not self._current_admission_id or self._write_pending:
            return
        order_id = _safe_int(row.get("id"))
        if not order_id:
            return
        old_datetime = _minute_floor_dt(_parse_datetime_value(row.get("datetime")))
        if old_datetime is None:
            CustomMessageBox.warning(self, "Время назначения", "Не удалось определить время назначения. Обновите протокол.")
            self.refresh_protocol(force=True)
            return
        text = str(row.get("text") or "").strip()
        if not text:
            CustomMessageBox.warning(self, "Ошибка", "Текст назначения не заполнен.")
            return
        dialog = TimeEditDialog("Время назначения", old_datetime, self, field_label="Время введения")
        if dialog.exec() != QDialog.Accepted:
            return
        order_datetime = dialog.datetime_text()
        if not self._validate_order_datetime_or_warn(order_datetime):
            self.refresh_protocol(force=True)
            return
        new_datetime = _minute_floor_dt(_parse_datetime_value(order_datetime))
        if new_datetime == old_datetime:
            return
        expected_revision = int(row.get("revision") or 0)
        self._write_pending = True
        self._set_protocol_write_controls_enabled(False)

        def operation():
            return self.operblock_service.update_order_text(
                self._current_admission_id,
                order_id,
                text,
                order_datetime,
                expected_revision=expected_revision,
            )

        write_description = f"operblock_update_order_time:{self._current_admission_id}:{order_id}"
        self._remember_local_write_refresh_suppression(write_description, {"orders"})
        self._enqueue_write(
            write_description,
            operation,
            on_success=lambda _result, oid=order_id, new_text=text, dt=order_datetime: self._on_order_edit_saved(
                oid,
                new_text,
                order_datetime=dt,
            ),
            on_error=lambda exc: self._on_order_mutation_error(exc),
        )

    def _delete_order(self, row: dict):
        if self.is_view_only_mode():
            return
        if not self._current_admission_id or self._write_pending:
            return
        order_id = _safe_int(row.get("id"))
        if not order_id:
            return
        text = (
            _build_order_text_for_display(str(row.get("drug_name") or ""), str(row.get("dose_text") or ""))
            if row.get("drug_name")
            else str(row.get("text") or "").strip()
        )
        reply = CustomMessageBox.question(
            self,
            "Удаление назначения",
            f"Удалить назначение?\n{text}",
            CustomMessageBox.Yes | CustomMessageBox.No,
            CustomMessageBox.No,
        )
        if reply != CustomMessageBox.Yes:
            return
        expected_revision = int(row.get("revision") or 0)
        self._write_pending = True
        self._set_protocol_write_controls_enabled(False)

        def operation():
            return self.operblock_service.delete_order(
                self._current_admission_id,
                order_id,
                expected_revision=expected_revision,
            )

        self._enqueue_write(
            f"operblock_delete_order:{self._current_admission_id}:{order_id}",
            operation,
            on_success=lambda _result: self._on_order_mutation_saved(),
            on_error=lambda exc: self._on_order_mutation_error(exc),
        )

    def _on_order_mutation_saved(self):
        self._write_pending = False
        self._set_protocol_write_controls_enabled(True)
        self._orders_force_top_on_next_apply = True
        self.refresh_protocol(force=True)

    def _on_order_mutation_error(self, exc: Exception):
        self._write_pending = False
        self._set_protocol_write_controls_enabled(True)
        title = "Конфликт данных" if isinstance(exc, (DataConflictError, OperBlockConflictError)) else "Ошибка назначения"
        CustomMessageBox.warning(self, title, str(exc))
        self.refresh_protocol(force=True)

    def _add_quick_order(self, drug_name: str, dose: str, *, kind: str = "bolus"):
        if self.is_view_only_mode():
            return
        if not self._current_admission_id or self._write_pending:
            return
        if not self._orders_tab_enabled():
            CustomMessageBox.warning(self, "Пособие не начато", "Назначения доступны только во время анестезиологического пособия.")
            self.refresh_protocol(force=True)
            return
        normalized_kind = "gas" if normalize_operblock_quick_order_kind(kind) == "gas" else "bolus"
        if normalized_kind == "gas":
            self._start_gas_infusion(
                drug_name,
                dose,
                payload={"manual_order": False, "quick_order": True},
                source_key="operblock_quick_gas_order",
            )
            return
        text = build_operblock_quick_order_text(drug_name, dose)
        if not text:
            return
        quick_scroll_state = self._remember_quick_orders_scroll_state()
        self._write_pending = True
        self._set_protocol_write_controls_enabled(False)
        self._restore_quick_orders_scroll_state_later(quick_scroll_state)

        def operation():
            return self.operblock_service.add_order(
                self._current_admission_id,
                text,
                return_row=True,
            )

        write_description = f"operblock_quick_{normalized_kind}_order:{self._current_admission_id}"
        self._remember_local_write_refresh_suppression(write_description, {"orders"})
        self._enqueue_write(
            write_description,
            operation,
            on_success=lambda result: self._on_quick_order_saved(result),
            on_error=lambda exc: self._on_quick_order_error(exc),
        )

    def _add_preset_bolus(self, preset: dict, dose: str):
        if self.is_view_only_mode():
            return
        if not self._current_admission_id or self._write_pending:
            return
        if not self._orders_tab_enabled():
            CustomMessageBox.warning(self, "Пособие не начато", "Назначения доступны только во время анестезиологического пособия.")
            self.refresh_protocol(force=True)
            return
        text = build_operblock_preset_bolus_text(preset, dose)
        if not text:
            return
        quick_scroll_state = self._remember_quick_orders_scroll_state()
        self._write_pending = True
        self._set_protocol_write_controls_enabled(False)
        self._restore_quick_orders_scroll_state_later(quick_scroll_state)
        preset_payload = build_operblock_preset_payload(preset)
        route_code = self._default_route_for_preset(preset)

        def operation():
            return self.operblock_service.add_order(
                self._current_admission_id,
                text,
                preset_payload=preset_payload,
                route=route_code,
                return_row=True,
            )

        write_description = f"operblock_preset_bolus:{self._current_admission_id}"
        self._remember_local_write_refresh_suppression(write_description, {"orders"})
        self._enqueue_write(
            write_description,
            operation,
            on_success=lambda result: self._on_quick_order_saved(result),
            on_error=lambda exc: self._on_quick_order_error(exc),
        )

    def _add_preset_gas(self, preset: dict, dose: str):
        if self.is_view_only_mode():
            return
        if not self._current_admission_id or self._write_pending:
            return
        if not self._orders_tab_enabled():
            CustomMessageBox.warning(self, "Пособие не начато", "Назначения доступны только во время анестезиологического пособия.")
            self.refresh_protocol(force=True)
            return
        drug_name = operblock_medication_preset_display_name(preset)
        preset_payload = build_operblock_preset_payload(preset)
        self._start_gas_infusion(
            drug_name,
            dose,
            payload=preset_payload,
            source_key="operblock_preset_gas",
        )

    def _start_timed_infusion_preset(self, preset: dict, dose_text: str = ""):
        if self.is_view_only_mode():
            return
        if not self._current_admission_id or self._write_pending:
            return
        drug_name = operblock_medication_preset_display_name(preset)
        if not drug_name:
            CustomMessageBox.warning(self, "Капельница", "Укажите препарат для капельницы.")
            return
        if not self._ensure_infusion_write_context_or_warn():
            return
        selected_dose = re.sub(r"\s+", " ", str(dose_text or "").strip())
        if not selected_dose:
            dose_options = _timed_infusion_dose_options(preset)
            selected_dose = dose_options[0] if dose_options else ""
        concentration_text = str(preset.get("concentration") or "").strip()
        dose_volume = _quick_order_dose_volume_ml(selected_dose, concentration_text)
        solvent_volume = _source_solvent_volume_ml(preset)
        total_volume = _timed_infusion_total_volume_ml(preset, selected_dose, concentration_text)
        volume = _volume_text_without_unit(total_volume)
        if not volume:
            if _quick_order_mass_dose_component(selected_dose):
                CustomMessageBox.warning(
                    self,
                    "Капельница",
                    "Для дозировки в мг/г укажите концентрацию препарата или объем капельницы в настройках быстрого назначения.",
                )
            else:
                CustomMessageBox.warning(self, "Капельница", "Укажите объем капельницы в настройках быстрого назначения.")
            return
        event_time = self._current_operation_event_time_text()
        if not self._validate_infusion_event_datetime_or_warn(event_time):
            return
        quick_scroll_state = self._remember_quick_orders_scroll_state()
        self._write_pending = True
        self._set_protocol_write_controls_enabled(False)
        self._restore_quick_orders_scroll_state_later(quick_scroll_state)
        preset_payload = build_operblock_preset_payload(preset)
        preset_payload["volume_ml"] = volume
        preset_payload["declared_total_volume_ml"] = volume
        if selected_dose:
            preset_payload["dose_text"] = selected_dose
            preset_payload["display_dose_text"] = _quick_order_dose_display_text(selected_dose, concentration_text)
            if dose_volume is not None:
                preset_payload["calculated_volume_ml"] = _volume_text_without_unit(dose_volume)
        if solvent_volume is not None:
            preset_payload["solvent_volume_ml"] = _volume_text_without_unit(solvent_volume)

        def operation():
            return self.operblock_service.start_infusion(
                self._current_admission_id,
                self._current_operation_case_id,
                drug_name,
                None,
                "",
                event_time,
                concentration_text=concentration_text,
                volume_ml=volume,
                payload=preset_payload,
                return_event=True,
            )

        write_description = f"operblock_timed_infusion:{self._current_admission_id}"
        self._remember_local_write_refresh_suppression(write_description, {"operblock_timeline_events"})
        self._enqueue_write(
            write_description,
            operation,
            on_success=lambda result: self._on_infusion_mutation_saved(result),
            on_error=lambda exc: self._on_infusion_mutation_error(exc),
        )

    def _add_timed_infusion_preset(self, preset: dict):
        self._start_timed_infusion_preset(preset, "")

    def _on_quick_order_saved(self, result=None):
        quick_scroll_state = getattr(self, "_pending_quick_orders_scroll_state", None)
        self._pending_quick_orders_scroll_state = None
        self._write_pending = False
        self._set_protocol_write_controls_enabled(True)
        if not self._apply_added_order_locally(dict(result or {}) if isinstance(result, dict) else {}):
            self.refresh_protocol(force=True)
        self._restore_quick_orders_scroll_state_later(quick_scroll_state)

    def _on_quick_order_error(self, exc: Exception):
        quick_scroll_state = getattr(self, "_pending_quick_orders_scroll_state", None)
        self._pending_quick_orders_scroll_state = None
        self._write_pending = False
        self._set_protocol_write_controls_enabled(True)
        title = "Конфликт данных" if isinstance(exc, (DataConflictError, OperBlockConflictError)) else "Ошибка сохранения"
        CustomMessageBox.warning(self, title, str(exc))
        self.refresh_protocol(force=True)
        self._restore_quick_orders_scroll_state_later(quick_scroll_state)
