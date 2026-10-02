from __future__ import annotations

from PySide6.QtWidgets import QDialog
from datetime import datetime
from rem_card.services.concurrency import DataConflictError
from rem_card.services.operblock_medication_presets import build_operblock_preset_payload
from rem_card.services.operblock_medication_presets import operblock_medication_preset_display_name
from rem_card.services.operblock_service import OperBlockConflictError
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.operblock_view.operblock_helpers import (
    _format_infusion_declared_volume,
    _format_infusion_rate,
    _gas_dose_text,
    _infusion_display_drug_name,
    _is_gas_infusion,
    _is_oxygen_infusion,
    _is_volume_only_infusion,
    _minute_floor_dt,
    _normalize_gas_dose_text,
    _normalize_oxygen_flow_text,
    _normalize_volume_ml_text,
    _oxygen_payload_fields,
    _parse_datetime_value,
    _safe_int,
    _split_infusion_rate_text,
)
from rem_card.ui.operblock_view.operblock_medication_edit_dialogs import (
    GasDoseDialog,
    InfusionRateDialog,
    InfusionStopDialog,
    InfusionVolumeDialog,
    TimeEditDialog,
)
from rem_card.ui.operblock_view.operblock_visual_primitives import (
    OPERBLOCK_INFUSION_CHANGE_BUTTON_TEXT,
)


class OperBlockInfusionActionsMixin:
    def _manual_order_kind(self) -> str:
        text = str(self.order_type_combo.currentText() if hasattr(self, "order_type_combo") else "").strip().casefold()
        if "газ" in text:
            return "gas"
        if "дозатор" in text:
            return "continuous_infusion"
        if "капель" in text:
            return "timed_infusion"
        return "bolus"

    def _update_manual_order_type_fields(self, *_args) -> None:
        kind = self._manual_order_kind()
        rate_visible = kind == "continuous_infusion"
        if hasattr(self, "order_rate_input"):
            self.order_rate_input.setVisible(rate_visible)
            self.order_rate_input.setEnabled(bool(not self._write_pending) and self._orders_tab_enabled())
        if hasattr(self, "order_dose_input"):
            if kind == "continuous_infusion":
                self.order_dose_input.setPlaceholderText("Доза (мг)")
            elif kind == "timed_infusion":
                self.order_dose_input.setPlaceholderText("Объем/доза (мл, мг)")
            elif kind == "gas":
                self.order_dose_input.setPlaceholderText("MAC или поток л/мин")
            else:
                self.order_dose_input.setPlaceholderText("Доза (мг, мл, %)")

    def _ensure_infusion_write_context_or_warn(self) -> bool:
        if not self._current_admission_id or not self._current_operation_case_id:
            return False
        if not self._current_case_active:
            CustomMessageBox.warning(self, "Случай закрыт", "Случай в операционной закрыт. Откройте его из архива или верните на стол.")
            self.refresh_protocol(force=True)
            return False
        if not self._current_anesthesia_active:
            CustomMessageBox.warning(self, "Пособие не начато", "Сначала нажмите «Начать пособие». До начала пособия назначения недоступны.")
            self.refresh_protocol(force=True)
            return False
        return True

    def _current_operation_event_time_text(self) -> str:
        now_dt = datetime.now().replace(second=0, microsecond=0)
        start_dt = _minute_floor_dt(self._current_anesthesia_start or self._current_operation_start)
        if start_dt and now_dt < start_dt:
            now_dt = start_dt
        return now_dt.isoformat(timespec="seconds")

    def _current_operation_event_datetime(self) -> datetime:
        return (
            _minute_floor_dt(_parse_datetime_value(self._current_operation_event_time_text()))
            or datetime.now().replace(second=0, microsecond=0)
        )

    @staticmethod
    def _local_iso_minute_text(value) -> str:
        event_dt = _minute_floor_dt(_parse_datetime_value(value))
        if event_dt is None:
            event_dt = datetime.now().replace(second=0, microsecond=0)
        return event_dt.isoformat(timespec="seconds")

    def _resolve_operation_event_time_text(self, time_text: str) -> str | None:
        clean_time = str(time_text or "").strip()
        if not clean_time:
            return None
        try:
            base_date = self._current_protocol_date or self._current_operation_start or datetime.now()
            event_dt = self.operblock_vitals_service.resolve_datetime(clean_time, base_date)
        except Exception:
            return None
        event_dt = _minute_floor_dt(event_dt)
        return event_dt.isoformat(timespec="seconds") if event_dt is not None else None

    @staticmethod
    def _infusion_identity(interval: dict) -> tuple[int | None, int | None]:
        payload = interval.get("payload") if isinstance(interval.get("payload"), dict) else {}
        start_event_id = _safe_int((payload or {}).get("start_event_id"))
        if start_event_id is None:
            interval_id = str(interval.get("interval_id") or "")
            if interval_id.startswith("infusion:"):
                start_event_id = _safe_int(interval_id.split(":", 1)[1])
        expected_revision = _safe_int((payload or {}).get("start_revision"))
        return start_event_id, expected_revision

    @staticmethod
    def _infusion_preset_payload(interval: dict) -> dict | None:
        payload = interval.get("payload") if isinstance(interval.get("payload"), dict) else {}
        result = {
            key: payload.get(key)
            for key in (
                "preset_id",
                "source_drug_id",
                "label",
                "display_name",
                "latin",
                "kind",
                "concentration",
                "solvent_id",
                "solvent_label",
                "solvent_volume_ml",
                "volume_ml",
                "dose_text",
                "display_dose_text",
                "gas_subtype",
                "is_oxygen",
                "oxygen_flow_lpm",
                "oxygen_flow_unit",
                "calculated_volume_ml",
                "declared_total_volume_ml",
                "duration_min",
                "card_color",
            )
            if payload.get(key) not in (None, "", [])
        }
        return result or None

    @staticmethod
    def _latest_infusion_event_datetime(interval: dict) -> datetime | None:
        latest_dt = _minute_floor_dt(_parse_datetime_value((interval or {}).get("start_time")))
        for history_key in ("rate_history", "dose_history"):
            for item in list((interval or {}).get(history_key) or []):
                event_dt = _minute_floor_dt(_parse_datetime_value((item or {}).get("event_time")))
                if event_dt is not None and (latest_dt is None or event_dt > latest_dt):
                    latest_dt = event_dt
        return latest_dt

    def _active_gas_interval(self, *, oxygen: bool | None = None) -> dict | None:
        snapshot = getattr(self, "_current_timeline_snapshot", None) or {}
        intervals = [
            dict(interval or {})
            for interval in snapshot.get("infusion_intervals") or []
            if str((interval or {}).get("status") or "") == "active" and _is_gas_infusion(interval or {})
        ]
        if oxygen is not None:
            intervals = [interval for interval in intervals if _is_oxygen_infusion(interval) == bool(oxygen)]
        if not intervals:
            return None
        intervals.sort(key=lambda item: _parse_datetime_value(item.get("start_time")) or datetime.max)
        return intervals[0]

    def _update_gas_dose_direct(
        self,
        interval: dict,
        dose_text: str,
        *,
        event_time: str | None = None,
        start_event_time: str | None = None,
        source_key: str = "operblock_update_gas_dose",
        on_saved=None,
    ) -> None:
        if self._write_pending or not self._ensure_infusion_write_context_or_warn():
            return
        start_event_id, expected_revision = self._infusion_identity(interval)
        if start_event_id is None or expected_revision is None:
            CustomMessageBox.warning(self, "Газ", "Не удалось проверить актуальность газа. Обновите протокол.")
            self.refresh_protocol(force=True)
            return
        is_oxygen = _is_oxygen_infusion(interval)
        clean_dose = _normalize_oxygen_flow_text(dose_text) if is_oxygen else _normalize_gas_dose_text(dose_text)
        if not clean_dose:
            if is_oxygen:
                CustomMessageBox.warning(self, "Кислород", "Укажите поток кислорода, например: 10 л/мин.")
            else:
                CustomMessageBox.warning(self, "Газ", "Укажите дозу газа, например: 0,7 MAC.")
            return
        change_event_time = event_time or self._current_operation_event_time_text()
        if not self._validate_infusion_event_datetime_or_warn(change_event_time):
            return
        if start_event_time is not None and not self._validate_infusion_event_datetime_or_warn(start_event_time):
            return
        current_dose = _gas_dose_text(interval)
        has_rate_artifacts = bool(
            interval.get("current_rate_value")
            or interval.get("current_rate_unit")
            or list(interval.get("rate_history") or [])
        )
        if clean_dose == current_dose and event_time is None and start_event_time is None and not has_rate_artifacts:
            return
        payload = self._infusion_preset_payload(interval) or {}
        payload = _oxygen_payload_fields(payload, clean_dose) if is_oxygen else payload
        payload["kind"] = "gas"
        payload["dose_text"] = clean_dose
        payload["display_dose_text"] = clean_dose
        quick_scroll_state = self._remember_quick_orders_scroll_state()
        self._write_pending = True
        self._set_protocol_write_controls_enabled(False)
        self._restore_quick_orders_scroll_state_later(quick_scroll_state)
        write_description = f"{source_key}:{start_event_id}"
        if on_saved is None:
            self._remember_local_write_refresh_suppression(write_description, {"operblock_timeline_events"})

        def operation():
            return self.operblock_service.change_gas_dose(
                start_event_id,
                expected_revision=expected_revision,
                dose_text=clean_dose,
                event_time=change_event_time,
                start_event_time=start_event_time,
                payload=payload,
            )

        self._enqueue_write(
            write_description,
            operation,
            on_success=(
                (lambda _result: on_saved())
                if on_saved is not None
                else (
                    lambda result, sid=start_event_id, dose=clean_dose, change_dt=change_event_time, start_dt=start_event_time, oxygen=is_oxygen: self._on_gas_dose_saved_locally(
                        result,
                        sid,
                        dose_text=dose,
                        change_event_time=change_dt,
                        start_event_time=start_dt,
                        is_oxygen=oxygen,
                    )
                )
            ),
            on_error=lambda exc: self._on_infusion_mutation_error(exc),
        )

    def _start_quick_infusion(
        self,
        drug_name: str,
        rate_text: str,
        *,
        concentration_text: str = "",
        preset_payload: dict | None = None,
    ):
        if self.is_view_only_mode():
            return
        if self._write_pending or not self._ensure_infusion_write_context_or_warn():
            return
        rate_value, rate_unit = _split_infusion_rate_text(rate_text)
        if not rate_value or not rate_unit:
            CustomMessageBox.warning(self, "Дозатор", "Укажите скорость в мл/час, например: 1 мл/час.")
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
                rate_value,
                rate_unit,
                event_time,
                concentration_text=concentration_text,
                payload=preset_payload,
                return_event=True,
            )

        write_description = f"operblock_start_infusion:{self._current_admission_id}"
        self._remember_local_write_refresh_suppression(write_description, {"operblock_timeline_events"})
        self._enqueue_write(
            write_description,
            operation,
            on_success=lambda result: self._on_infusion_mutation_saved(result),
            on_error=lambda exc: self._on_infusion_mutation_error(exc),
        )

    def _start_preset_infusion(self, preset: dict, rate_text: str):
        drug_name = operblock_medication_preset_display_name(preset)
        if not drug_name:
            CustomMessageBox.warning(self, "Дозатор", "Укажите препарат для дозатора.")
            return
        self._start_quick_infusion(
            drug_name,
            rate_text,
            concentration_text=str(preset.get("concentration") or "").strip(),
            preset_payload=build_operblock_preset_payload(preset),
        )

    def _edit_infusion_start_time(self, interval: dict):
        if self._write_pending or not self._ensure_infusion_write_context_or_warn():
            return
        start_event_id, expected_revision = self._infusion_identity(interval)
        if start_event_id is None or expected_revision is None:
            CustomMessageBox.warning(self, "Дозатор", "Не удалось проверить актуальность дозатора. Обновите протокол.")
            self.refresh_protocol(force=True)
            return
        old_start_dt = _minute_floor_dt(_parse_datetime_value(interval.get("start_time")))
        if old_start_dt is None:
            CustomMessageBox.warning(self, "Дозатор", "Не удалось определить время начала дозатора. Обновите протокол.")
            self.refresh_protocol(force=True)
            return
        drug_name = _infusion_display_drug_name(interval, "Дозатор")
        dialog = TimeEditDialog(f"Время начала: {drug_name}", old_start_dt, self, field_label="Время начала")
        if dialog.exec() != QDialog.Accepted:
            return
        event_time = dialog.datetime_text()
        if not self._validate_infusion_event_datetime_or_warn(event_time):
            return
        new_start_dt = _minute_floor_dt(_parse_datetime_value(event_time))
        if new_start_dt == old_start_dt:
            return
        self._write_pending = True
        self._set_protocol_write_controls_enabled(False)

        def operation():
            return self.operblock_service.update_infusion_start_time(
                start_event_id,
                expected_revision=expected_revision,
                event_time=event_time,
            )

        write_description = f"operblock_update_infusion_start:{start_event_id}"
        self._remember_local_write_refresh_suppression(write_description, {"operblock_timeline_events"})
        self._enqueue_write(
            write_description,
            operation,
            on_success=lambda _result, sid=start_event_id, dt=event_time: self._on_infusion_start_time_saved_locally(sid, dt),
            on_error=lambda exc: self._on_infusion_mutation_error(exc),
        )

    def _change_gas_dose(self, interval: dict, *, include_time: bool = False):
        if self.is_view_only_mode():
            return
        if self._write_pending or not self._ensure_infusion_write_context_or_warn():
            return
        start_event_id, expected_revision = self._infusion_identity(interval)
        if start_event_id is None or expected_revision is None:
            CustomMessageBox.warning(self, "Газ", "Не удалось проверить актуальность газа. Обновите протокол.")
            self.refresh_protocol(force=True)
            return
        is_oxygen = _is_oxygen_infusion(interval)
        current_dose = _gas_dose_text(interval)
        old_start_dt = _minute_floor_dt(_parse_datetime_value(interval.get("start_time")))
        if include_time and old_start_dt is None:
            CustomMessageBox.warning(self, "Газ", "Не удалось определить время начала газа. Обновите протокол.")
            self.refresh_protocol(force=True)
            return
        drug_name = _infusion_display_drug_name(interval, "Газ")
        dialog = GasDoseDialog(
            drug_name,
            current_dose,
            self,
            start_datetime=old_start_dt,
            min_datetime=self._current_anesthesia_start or self._current_operation_start,
            max_datetime=self._current_anesthesia_end,
            show_time=include_time,
            action_text="Сохранить",
            payload=interval.get("payload") if isinstance(interval.get("payload"), dict) else None,
            is_oxygen=is_oxygen,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        dose_text = _normalize_oxygen_flow_text(dialog.volume_text()) if is_oxygen else _normalize_gas_dose_text(dialog.volume_text())
        if not dose_text:
            if is_oxygen:
                CustomMessageBox.warning(self, "Кислород", "Укажите поток кислорода, например: 10 л/мин.")
            else:
                CustomMessageBox.warning(self, "Газ", "Укажите дозу газа, например: 0,7 MAC.")
            return
        start_event_time = None
        new_start_dt = old_start_dt
        if include_time:
            start_event_time = self._resolve_operation_event_time_text(dialog.start_time_text())
            if not self._validate_infusion_event_datetime_or_warn(start_event_time):
                return
            new_start_dt = _minute_floor_dt(_parse_datetime_value(start_event_time))
        has_rate_artifacts = bool(
            interval.get("current_rate_value")
            or interval.get("current_rate_unit")
            or list(interval.get("rate_history") or [])
        )
        dose_changed = dose_text != current_dose or has_rate_artifacts
        start_changed = bool(include_time and new_start_dt != old_start_dt)
        if not dose_changed and not start_changed:
            return
        if not dose_changed and start_changed:
            self._write_pending = True
            self._set_protocol_write_controls_enabled(False)

            def operation():
                return self.operblock_service.update_infusion_start_time(
                    start_event_id,
                    expected_revision=expected_revision,
                    event_time=start_event_time,
                )

            write_description = f"operblock_update_gas_start:{start_event_id}"
            self._remember_local_write_refresh_suppression(write_description, {"operblock_timeline_events"})
            self._enqueue_write(
                write_description,
                operation,
                on_success=lambda _result, sid=start_event_id, dt=start_event_time: self._on_infusion_start_time_saved_locally(sid, dt),
                on_error=lambda exc: self._on_infusion_mutation_error(exc),
            )
            return
        self._update_gas_dose_direct(
            interval,
            dose_text,
            start_event_time=start_event_time if start_changed else None,
            source_key="operblock_update_gas_dose",
        )

    def _change_infusion_rate(self, interval: dict, *, include_time: bool = False):
        if self.is_view_only_mode():
            return
        if self._write_pending or not self._ensure_infusion_write_context_or_warn():
            return
        start_event_id, expected_revision = self._infusion_identity(interval)
        if start_event_id is None or expected_revision is None:
            CustomMessageBox.warning(self, "Дозатор", "Не удалось проверить актуальность дозатора. Обновите протокол.")
            self.refresh_protocol(force=True)
            return
        current_rate = _format_infusion_rate(interval.get("current_rate_value"), interval.get("current_rate_unit"))
        old_start_dt = _minute_floor_dt(_parse_datetime_value(interval.get("start_time")))
        if include_time and old_start_dt is None:
            CustomMessageBox.warning(self, "Дозатор", "Не удалось определить время начала дозатора. Обновите протокол.")
            self.refresh_protocol(force=True)
            return
        dialog_title = "Изменить дозатор" if include_time else f"{OPERBLOCK_INFUSION_CHANGE_BUTTON_TEXT} скорость"
        dialog = InfusionRateDialog(
            dialog_title,
            current_rate,
            self,
            drug_name=_infusion_display_drug_name(interval, "Дозатор"),
            start_datetime=old_start_dt if include_time else None,
            min_datetime=self._current_anesthesia_start or self._current_operation_start,
            max_datetime=self._current_anesthesia_end,
            action_text="Сохранить",
        )
        if dialog.exec() != QDialog.Accepted:
            return
        rate_value, rate_unit = _split_infusion_rate_text(dialog.rate_text())
        if not rate_value or not rate_unit:
            CustomMessageBox.warning(self, "Дозатор", "Укажите скорость в мл/час, например: 1 мл/час.")
            return
        normalized_rate = _format_infusion_rate(rate_value, rate_unit)
        rate_changed = normalized_rate != current_rate
        start_event_time = None
        new_start_dt = old_start_dt
        if include_time:
            start_event_time = self._resolve_operation_event_time_text(dialog.start_time_text())
            if not self._validate_infusion_event_datetime_or_warn(start_event_time):
                return
            new_start_dt = _minute_floor_dt(_parse_datetime_value(start_event_time))
        start_changed = bool(include_time and new_start_dt != old_start_dt)
        if not rate_changed and not start_changed:
            return
        change_event_time = self._current_operation_event_time_text()
        self._write_pending = True
        self._set_protocol_write_controls_enabled(False)

        def operation():
            if not rate_changed:
                return self.operblock_service.update_infusion_start_time(
                    start_event_id,
                    expected_revision=expected_revision,
                    event_time=start_event_time,
                )
            return self.operblock_service.change_infusion_rate(
                start_event_id,
                expected_revision=expected_revision,
                new_rate_value=rate_value,
                new_rate_unit=rate_unit,
                event_time=change_event_time,
                start_event_time=start_event_time if start_changed else None,
                payload=self._infusion_preset_payload(interval),
            )

        write_description = f"operblock_change_infusion:{start_event_id}"
        self._remember_local_write_refresh_suppression(write_description, {"operblock_timeline_events"})
        self._enqueue_write(
            write_description,
            operation,
            on_success=(
                (lambda _result, sid=start_event_id, dt=start_event_time: self._on_infusion_start_time_saved_locally(sid, dt))
                if not rate_changed
                else (
                    lambda result, sid=start_event_id, rv=rate_value, ru=rate_unit, change_dt=change_event_time, start_dt=start_event_time if start_changed else None: self._on_infusion_rate_saved_locally(
                        result,
                        sid,
                        rate_value=rv,
                        rate_unit=ru,
                        change_event_time=change_dt,
                        start_event_time=start_dt,
                    )
                )
            ),
            on_error=lambda exc: self._on_infusion_mutation_error(exc),
        )

    def _change_infusion_volume(self, interval: dict, *, include_time: bool = False):
        if self.is_view_only_mode():
            return
        if self._write_pending or not self._ensure_infusion_write_context_or_warn():
            return
        start_event_id, expected_revision = self._infusion_identity(interval)
        if start_event_id is None or expected_revision is None:
            CustomMessageBox.warning(self, "Капельница", "Не удалось проверить актуальность капельницы. Обновите протокол.")
            self.refresh_protocol(force=True)
            return
        current_volume = _format_infusion_declared_volume(interval).replace(" мл", "")
        old_start_dt = _minute_floor_dt(_parse_datetime_value(interval.get("start_time")))
        if include_time and old_start_dt is None:
            CustomMessageBox.warning(self, "Капельница", "Не удалось определить время начала капельницы. Обновите протокол.")
            self.refresh_protocol(force=True)
            return
        drug_name = _infusion_display_drug_name(interval, "Капельница")
        dialog = InfusionVolumeDialog(
            f"{'Изменить капельницу' if include_time else 'Правка объема'}: {drug_name}",
            current_volume,
            self,
            drug_name=_infusion_display_drug_name(interval, "Капельница"),
            start_datetime=old_start_dt,
            min_datetime=self._current_anesthesia_start or self._current_operation_start,
            max_datetime=self._current_anesthesia_end,
            show_time=include_time,
            action_text="Сохранить",
            field_label="Объем: мл",
        )
        if dialog.exec() != QDialog.Accepted:
            return
        volume = _normalize_volume_ml_text(dialog.volume_text())
        if not volume:
            CustomMessageBox.warning(self, "Капельница", "Укажите объем в мл, например: 200 мл.")
            return
        event_time = None
        new_start_dt = old_start_dt
        if include_time:
            event_time = self._resolve_operation_event_time_text(dialog.start_time_text())
            if not self._validate_infusion_event_datetime_or_warn(event_time):
                return
            new_start_dt = _minute_floor_dt(_parse_datetime_value(event_time))
        if volume == current_volume and (not include_time or new_start_dt == old_start_dt):
            return
        self._write_pending = True
        self._set_protocol_write_controls_enabled(False)

        def operation():
            return self.operblock_service.update_infusion_volume(
                start_event_id,
                expected_revision=expected_revision,
                volume_ml=volume,
                event_time=event_time,
                payload=self._infusion_preset_payload(interval),
            )

        write_description = f"operblock_update_infusion_volume:{start_event_id}"
        self._remember_local_write_refresh_suppression(write_description, {"operblock_timeline_events"})
        self._enqueue_write(
            write_description,
            operation,
            on_success=lambda _result, sid=start_event_id, vol=volume, dt=event_time: self._on_infusion_volume_saved_locally(
                sid,
                volume_ml=vol,
                event_time=dt,
            ),
            on_error=lambda exc: self._on_infusion_mutation_error(exc),
        )

    def _stop_infusion(self, interval: dict):
        if self.is_view_only_mode():
            return
        if self._write_pending or not self._ensure_infusion_write_context_or_warn():
            return
        start_event_id, expected_revision = self._infusion_identity(interval)
        if start_event_id is None or expected_revision is None:
            CustomMessageBox.warning(self, "Дозатор", "Не удалось проверить актуальность дозатора. Обновите протокол.")
            self.refresh_protocol(force=True)
            return
        drug_name = _infusion_display_drug_name(interval, "назначение")
        start_dt = _minute_floor_dt(_parse_datetime_value(interval.get("start_time")))
        if start_dt is None:
            CustomMessageBox.warning(self, "Время остановки", "Не удалось определить время начала назначения. Обновите протокол.")
            self.refresh_protocol(force=True)
            return
        if _is_gas_infusion(interval):
            infusion_kind = "gas"
        elif _is_volume_only_infusion(interval):
            infusion_kind = "volume"
        else:
            infusion_kind = "rate"
        dialog = InfusionStopDialog(
            drug_name,
            self,
            start_datetime=self._current_operation_event_datetime(),
            min_datetime=self._latest_infusion_event_datetime(interval) or start_dt,
            max_datetime=self._current_anesthesia_end if not self._current_anesthesia_active else None,
            infusion_kind=infusion_kind,
            payload=interval.get("payload") if isinstance(interval.get("payload"), dict) else None,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        event_time = dialog.datetime_text()
        if not self._validate_infusion_stop_datetime_or_warn(event_time, interval):
            return
        self._write_pending = True
        self._set_protocol_write_controls_enabled(False)

        def operation():
            return self.operblock_service.stop_infusion(
                start_event_id,
                expected_revision=expected_revision,
                event_time=event_time,
                payload=self._infusion_preset_payload(interval),
            )

        self._enqueue_write(
            f"operblock_stop_infusion:{start_event_id}",
            operation,
            on_success=lambda _result: self._on_infusion_mutation_saved(),
            on_error=lambda exc: self._on_infusion_mutation_error(exc),
        )

    def _delete_infusion(self, interval: dict):
        if self.is_view_only_mode():
            return
        if self._write_pending or not self._ensure_infusion_write_context_or_warn():
            return
        start_event_id, expected_revision = self._infusion_identity(interval)
        if start_event_id is None or expected_revision is None:
            CustomMessageBox.warning(self, "Дозатор", "Не удалось проверить актуальность дозатора. Обновите протокол.")
            self.refresh_protocol(force=True)
            return
        drug_name = _infusion_display_drug_name(interval, "назначение")
        is_active = str(interval.get("status") or "") == "active"
        title = "Удалить активное назначение" if is_active else "Удаление назначения"
        message = (
            f"Удалить активное назначение из истории и графика?\n{drug_name}"
            if is_active
            else f"Удалить назначение из истории и графика?\n{drug_name}"
        )
        reply = CustomMessageBox.question(
            self,
            title,
            message,
            CustomMessageBox.Yes | CustomMessageBox.No,
            CustomMessageBox.No,
        )
        if reply != CustomMessageBox.Yes:
            return
        self._write_pending = True
        self._set_protocol_write_controls_enabled(False)

        def operation():
            return self.operblock_service.delete_infusion(
                start_event_id,
                expected_revision=expected_revision,
            )

        self._enqueue_write(
            f"operblock_delete_infusion:{start_event_id}",
            operation,
            on_success=lambda _result: self._on_infusion_mutation_saved(),
            on_error=lambda exc: self._on_infusion_mutation_error(exc),
        )

    def _on_infusion_mutation_saved(self, result=None):
        quick_scroll_state = getattr(self, "_pending_quick_orders_scroll_state", None)
        self._pending_quick_orders_scroll_state = None
        self._write_pending = False
        self._set_protocol_write_controls_enabled(True)
        if not self._apply_started_infusion_locally(dict(result or {}) if isinstance(result, dict) else {}):
            self.refresh_protocol(force=True)
        self._restore_quick_orders_scroll_state_later(quick_scroll_state)

    def _on_infusion_mutation_error(self, exc: Exception):
        quick_scroll_state = getattr(self, "_pending_quick_orders_scroll_state", None)
        self._pending_quick_orders_scroll_state = None
        self._write_pending = False
        self._set_protocol_write_controls_enabled(True)
        title = "Конфликт данных" if isinstance(exc, (DataConflictError, OperBlockConflictError)) else "Ошибка назначения"
        CustomMessageBox.warning(self, title, str(exc))
        self.refresh_protocol(force=True)
        self._restore_quick_orders_scroll_state_later(quick_scroll_state)
