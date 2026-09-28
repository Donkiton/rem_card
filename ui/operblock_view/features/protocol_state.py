from __future__ import annotations

from PySide6.QtCore import QTimer
from datetime import datetime
from rem_card.app import operblock_startup_metrics
from rem_card.app.local_metrics import record_metric
from rem_card.app.logger import logger
from rem_card.services.operblock_anesthesia_types import normalize_operblock_anesthesia_type_label
from rem_card.services.operblock_team import normalize_operblock_team_text
from rem_card.ui.styles.theme_runtime import set_widget_style
import time
from rem_card.ui.operblock_view.operblock_helpers import (
    _format_main_remcard_status_text,
    _format_protocol_started_at,
    _minute_floor_dt,
    _parse_datetime_value,
)
from rem_card.ui.operblock_view.operblock_visual_primitives import (
    OPERBLOCK_CHART_EXPAND_THRESHOLD_MINUTES,
    OPERBLOCK_INITIAL_CHART_HOURS,
    OPERBLOCK_MAX_CHART_HOURS,
    OPERBLOCK_VITAL_SETTINGS,
)


class OperBlockProtocolStateMixin:
    def _open_protocol(self, operation_case_id: int):
        if self._is_closing:
            return
        action_info = self._start_opblock_action_diagnostics(f"operblock_open_protocol:{int(operation_case_id)}")
        loading_key = self._show_operblock_loading(
            "Открытие протокола операции...",
            key="open-protocol",
            auto_hide_ms=30000,
        )
        try:
            first_open = self.protocol_page is None
            first_open_started = operblock_startup_metrics.timer_start() if first_open else None
            if not self._ensure_protocol_page_created():
                return
            self._current_operation_case_id = int(operation_case_id)
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
            self._current_protocol_date = datetime.now()
            self._vitals_context_key = None
            self._current_orders_rows = []
            self._current_timeline_snapshot = None
            self._current_chart_vitals = []
            self._pending_orders_snapshot = {"orders": []}
            if getattr(self, "vitals_chart", None) and hasattr(self.vitals_chart, "set_timeline_snapshot"):
                self.vitals_chart.set_timeline_snapshot(None, None, force=True)
            if getattr(self, "_orders_tab_built", False):
                self._apply_active_infusions()
                self._apply_orders({"orders": []})
            self._protocol_hash = ""
            self.operblock_vitals_service.set_operation_context(
                operation_case_id=self._current_operation_case_id,
                admission_id=None,
                started_at=None,
                ended_at=None,
            )
            self._set_protocol_chrome(True)
            self.stack.setCurrentWidget(self.protocol_page)
            self._preload_operblock_chart_module()
            self._schedule_current_protocol_tab_ready(150)
            self.refresh_protocol(force=True, loading_message="Загрузка протокола операции...")
            if first_open:
                operblock_startup_metrics.record_since(
                    "first_open_protocol_ms",
                    first_open_started,
                    source="operblock_widget",
                )
        finally:
            self._finish_opblock_action_diagnostics(action_info, "success")
            self._hide_operblock_loading(loading_key)

    def _apply_protocol_snapshot(self, snapshot: dict):
        header = snapshot.get("header") or {}
        self._current_admission_id = int(header.get("admission_id") or 0)
        table_name = str(header.get("table_display_name") or "").strip()
        self.protocol_info_header_label.setText(f"Информация - {table_name}" if table_name else "Информация")
        started_at = header.get("started_at")
        ended_at = header.get("ended_at")
        self.protocol_started_label.setText(_format_protocol_started_at(started_at))
        self._current_operation_start = _minute_floor_dt(_parse_datetime_value(started_at)) or _minute_floor_dt(datetime.now())
        self._current_operation_end = _parse_datetime_value(ended_at)
        self._current_protocol_date = self._current_operation_start
        self.protocol_patient_label.setText(header.get("full_name") or "Неизвестно")
        self.protocol_history_label.setText(f"№ {header.get('history_number') or '—'}")
        self.protocol_age_label.setText(str(header.get("age") or "—"))
        diagnosis_text = header.get("diagnosis_text") or "—"
        diagnosis_code = header.get("diagnosis_code")
        diagnosis_line = f"{diagnosis_code}: {diagnosis_text}" if diagnosis_code else diagnosis_text
        self.protocol_diagnosis_label.set_full_text(f"Диагноз: {diagnosis_line}")
        self._current_protocol_display = str(header.get("protocol_display") or "").strip()
        self._update_protocol_status_label(started_at, active=header.get("status") == "active")
        self._current_case_active = header.get("status") == "active"
        self._current_operation_has_vitals = bool((snapshot.get("vitals") or {}).get("vitals"))
        self._apply_stage_state(header.get("stage_state") or {})
        latest = header.get("latest") or {}
        self._update_latest_badges(latest)
        self.operblock_vitals_service.set_operation_context(
            operation_case_id=self._current_operation_case_id,
            admission_id=self._current_admission_id,
            started_at=self._current_operation_start,
            ended_at=self._current_operation_end,
        )
        orders_snapshot = snapshot.get("orders") or {}
        self._current_orders_rows = [dict(row or {}) for row in orders_snapshot.get("orders") or []]
        self._current_timeline_snapshot = dict(snapshot.get("timeline") or {})
        self._current_chart_vitals = list(snapshot.get("chart_vitals") or [])
        self._pending_orders_snapshot = dict(orders_snapshot or {})
        if getattr(self, "_orders_tab_built", False):
            self._apply_active_infusions()
        self._set_vitals_context(self._current_admission_id)
        if getattr(self, "_vitals_tab_built", False):
            self._update_vitals_chart()
        if getattr(self, "_orders_tab_built", False):
            self._apply_orders(orders_snapshot)
        self._apply_protocol_controls_state()

    def _update_protocol_status_label(self, started_at, *, active: bool):
        text, color = _format_main_remcard_status_text(started_at, active=active)
        self.protocol_status_label.setText(text)
        set_widget_style(self.protocol_status_label, f"background-color: {color}; color: white; font-weight: bold; border-radius: 4px; padding: 2px;")

    def _update_latest_badges(self, latest: dict):
        ad = str(latest.get("ad") or "-/-")
        pulse = latest.get("pulse")
        spo2 = latest.get("spo2")
        self.badge_ad.set_value(ad)
        self.badge_pulse.set_value(str(pulse if pulse is not None else "-"))
        self.badge_spo2.set_value(f"{int(spo2)}%" if spo2 is not None else "-%")

    def _apply_stage_state(self, stage_state: dict):
        self._current_stage_state = dict(stage_state or {})
        self._current_anesthesia_active = bool(self._current_stage_state.get("anesthesia_active"))
        self._current_surgery_active = bool(self._current_stage_state.get("surgery_active"))
        self._current_anesthesia_start = _minute_floor_dt(
            _parse_datetime_value(self._current_stage_state.get("current_anesthesia_start"))
            or _parse_datetime_value(self._current_stage_state.get("last_anesthesia_start"))
        )
        self._current_anesthesia_end = _minute_floor_dt(_parse_datetime_value(self._current_stage_state.get("last_anesthesia_end")))
        self._current_surgery_start = _minute_floor_dt(
            _parse_datetime_value(self._current_stage_state.get("current_surgery_start"))
            or _parse_datetime_value(self._current_stage_state.get("last_surgery_start"))
        )
        self._current_surgery_end = _minute_floor_dt(_parse_datetime_value(self._current_stage_state.get("last_surgery_end")))
        self._current_anesthesia_assistance_type = normalize_operblock_anesthesia_type_label(
            self._current_stage_state.get("current_anesthesia_assistance_type")
            or self._current_stage_state.get("last_anesthesia_assistance_type")
            or self._current_stage_state.get("first_anesthesia_assistance_type")
            or self._current_stage_state.get("planned_anesthesia_assistance_type")
        )
        self._current_operation_name = normalize_operblock_team_text(
            self._current_stage_state.get("current_operation_name")
            or self._current_stage_state.get("last_operation_name")
            or self._current_stage_state.get("first_operation_name")
        )
        self._update_protocol_title_label()
        self._update_operblock_staff_legend()
        self._apply_protocol_controls_state()

    def _apply_protocol_controls_state(self):
        case_active = bool(getattr(self, "_current_case_active", False))
        aid_active = bool(getattr(self, "_current_anesthesia_active", False))
        surgery_active = bool(getattr(self, "_current_surgery_active", False))
        stages_available = self._operation_stages_available()
        view_only = self.is_view_only_mode()
        write_enabled = case_active and not self._write_pending and not view_only
        if hasattr(self, "start_anesthesia_button"):
            has_initial_vitals = bool(getattr(self, "_current_operation_has_vitals", False))
            start_prep_pending = bool(getattr(self, "_start_anesthesia_prep_pending", False))
            self.start_anesthesia_button.setEnabled(
                write_enabled and not aid_active and has_initial_vitals and not start_prep_pending
            )
            if write_enabled and not aid_active and not has_initial_vitals:
                self.start_anesthesia_button.setToolTip(
                    "Введите исходные витальные показатели, чтобы начать пособие."
                )
            else:
                self.start_anesthesia_button.setToolTip("")
            self.end_anesthesia_button.setEnabled(write_enabled and aid_active)
            self.start_surgery_button.setEnabled(write_enabled and aid_active and not surgery_active)
            self.operation_stages_button.setEnabled(write_enabled and stages_available)
            self.close_case_button.setEnabled(write_enabled and aid_active and surgery_active)
            self.release_table_button.setEnabled(write_enabled and not aid_active)
            if hasattr(self, "report_button"):
                self.report_button.setEnabled(bool(self._current_operation_case_id))
        if getattr(self, "vitals_input", None) is not None:
            try:
                self.vitals_input.set_forced_read_only(view_only or not case_active)
            except Exception:
                pass
            if hasattr(self.vitals_input, "save_btn"):
                self.vitals_input.save_btn.setEnabled(write_enabled)
            if hasattr(self.vitals_input, "undo_btn"):
                self.vitals_input.undo_btn.setEnabled(write_enabled)

        orders_tab_available = case_active and aid_active
        orders_controls_enabled = write_enabled and aid_active
        if hasattr(self, "orders_tab_button"):
            orders_visible = bool(getattr(self, "_protocol_tab_visible", {}).get("orders", True))
            self.orders_tab_button.setEnabled(orders_visible and orders_tab_available)
            if (
                self.content_stack is not None
                and (not orders_visible or not orders_tab_available)
                and self.content_stack.currentIndex() == 1
            ):
                self._set_protocol_tab_by_id("vitals")
            self._ensure_visible_protocol_tab()
        self._set_orders_entry_controls_enabled(orders_controls_enabled)
        if view_only and orders_tab_available:
            self._set_quick_order_buttons_enabled(True)

    def _set_orders_entry_controls_enabled(self, enabled: bool):
        for widget_name in ("order_input", "order_dose_input", "order_type_combo", "save_order_button"):
            widget = getattr(self, widget_name, None)
            if widget is not None:
                widget.setEnabled(bool(enabled))
        self._set_quick_order_buttons_enabled(enabled)
        self._set_order_action_buttons_enabled(enabled)
        self._set_infusion_action_buttons_enabled(enabled)

    def _set_vitals_context(self, admission_id: int):
        if not admission_id or not getattr(self, "vitals_input", None):
            return
        context_key = (
            int(admission_id),
            int(self._current_operation_case_id or 0),
            self._current_protocol_date.isoformat(timespec="minutes"),
        )
        if self._vitals_context_key == context_key:
            return
        self._vitals_context_key = context_key
        self.vitals_input.set_context(int(admission_id), self._current_protocol_date)

    def _update_vitals_chart(self):
        chart = getattr(self, "vitals_chart", None)
        if not self._current_admission_id or not self._current_operation_case_id or chart is None:
            return
        try:
            start_dt = self._current_operation_start or self._current_protocol_date
            vitals = list(getattr(self, "_current_chart_vitals", []) or [])
            anesthesia_started_at = self._first_anesthesia_start_for_chart()
            timeline_transform = type(chart).build_operation_timeline_transform(anesthesia_started_at, vitals)
            display_start_dt = timeline_transform.display_origin_at or start_dt
            visible_hours = self._calculate_operblock_chart_hours(
                display_start_dt,
                vitals,
                self._current_operation_end,
                timeline_transform=timeline_transform,
                timeline_snapshot=getattr(self, "_current_timeline_snapshot", None),
            )
            chart.admission_id = self._current_admission_id
            if hasattr(chart, "set_operation_timeline_model"):
                chart.set_operation_timeline_model(timeline_transform)
            chart.set_visible_hours(visible_hours)
            chart.update_data(vitals, display_start_dt, active_intervals=None)
            if hasattr(chart, "set_timeline_snapshot"):
                chart.set_timeline_snapshot(
                    getattr(self, "_current_timeline_snapshot", None),
                    display_start_dt,
                    force=True,
                )
            elif hasattr(chart, "set_operation_orders"):
                chart.set_operation_orders(getattr(self, "_current_orders_rows", []), display_start_dt, force=True)
            if getattr(self, "vitals_legend_sector", None):
                self.vitals_legend_sector.update_legend(OPERBLOCK_VITAL_SETTINGS)
        except Exception as exc:
            logger.error("operblock vitals chart refresh failed: %s", exc, exc_info=True)

    def _first_anesthesia_start_for_chart(self) -> datetime | None:
        state = getattr(self, "_current_stage_state", {}) or {}
        candidates = [
            state.get("first_anesthesia_start"),
            state.get("current_anesthesia_start"),
            state.get("last_anesthesia_start"),
        ]
        for value in candidates:
            parsed = _minute_floor_dt(_parse_datetime_value(value))
            if parsed is not None:
                return parsed
        return None

    @staticmethod
    def _timeline_extent_datetimes(timeline_snapshot: dict | None) -> list[datetime]:
        snapshot = timeline_snapshot or {}
        result: list[datetime] = []

        def add(value) -> None:
            parsed = _minute_floor_dt(_parse_datetime_value(value))
            if isinstance(parsed, datetime):
                result.append(parsed)

        for event in snapshot.get("operation_events") or []:
            add((event or {}).get("event_time"))
        for event in snapshot.get("bolus_events") or []:
            add((event or {}).get("event_time"))

        now_dt = datetime.now().replace(second=0, microsecond=0)
        for interval in snapshot.get("infusion_intervals") or []:
            interval = interval or {}
            add(interval.get("start_time"))
            add(interval.get("end_time"))
            for history in interval.get("rate_history") or []:
                add((history or {}).get("event_time"))
            if str(interval.get("status") or "") == "active":
                add(now_dt)
        return result

    @staticmethod
    def _calculate_operblock_chart_hours(
        start_dt: datetime | None,
        vitals,
        ended_at: datetime | None = None,
        *,
        timeline_transform: object | None = None,
        timeline_snapshot: dict | None = None,
    ) -> int:
        from rem_card.ui.operblock_view.operblock_main_widget import OperBlockMainWidget

        if start_dt is None:
            return OPERBLOCK_INITIAL_CHART_HOURS
        if timeline_transform and timeline_transform.display_origin_at:
            elapsed_hours = 0.0
            for vital in vitals or []:
                timestamp = getattr(vital, "timestamp", None)
                x = timeline_transform.display_hours_for_vital(timestamp, fallback_start=start_dt)
                if x is not None:
                    elapsed_hours = max(elapsed_hours, float(x))
            if ended_at:
                x = timeline_transform.display_hours_for(ended_at, fallback_start=start_dt)
                if x is not None:
                    elapsed_hours = max(elapsed_hours, float(x))
            for event_dt in OperBlockMainWidget._timeline_extent_datetimes(timeline_snapshot):
                x = timeline_transform.display_hours_for(event_dt, fallback_start=start_dt)
                if x is not None:
                    elapsed_hours = max(elapsed_hours, float(x))
        else:
            max_dt = ended_at if ended_at and ended_at > start_dt else start_dt
            for vital in vitals or []:
                timestamp = getattr(vital, "timestamp", None)
                if isinstance(timestamp, datetime) and timestamp > max_dt:
                    max_dt = timestamp
            for event_dt in OperBlockMainWidget._timeline_extent_datetimes(timeline_snapshot):
                if event_dt > max_dt:
                    max_dt = event_dt
            elapsed_hours = max(0.0, (max_dt - start_dt).total_seconds() / 3600.0)
        visible_hours = OPERBLOCK_INITIAL_CHART_HOURS
        threshold_hours = OPERBLOCK_CHART_EXPAND_THRESHOLD_MINUTES / 60.0
        while elapsed_hours >= visible_hours - threshold_hours and visible_hours < OPERBLOCK_MAX_CHART_HOURS:
            visible_hours += 1
        return visible_hours

    def _on_standard_vitals_changed(self, change=None):
        started = time.perf_counter()
        payload = dict(change or {}) if isinstance(change, dict) else {}
        action = str(payload.get("action") or "")
        chart_vitals = list(getattr(self, "_current_chart_vitals", []) or [])
        if action == "upsert":
            vital = payload.get("vital")
            vital_id = getattr(vital, "id", None)
            vital_timestamp = getattr(vital, "timestamp", None)
            replaced = False
            for index, current in enumerate(chart_vitals):
                current_id = getattr(current, "id", None)
                current_timestamp = getattr(current, "timestamp", None)
                if (
                    vital_id is not None
                    and current_id is not None
                    and int(current_id) == int(vital_id)
                ) or (
                    isinstance(vital_timestamp, datetime)
                    and isinstance(current_timestamp, datetime)
                    and _minute_floor_dt(current_timestamp) == _minute_floor_dt(vital_timestamp)
                ):
                    chart_vitals[index] = vital
                    replaced = True
                    break
            if vital is not None and not replaced:
                chart_vitals.append(vital)
            chart_vitals.sort(
                key=lambda item: (
                    getattr(item, "timestamp", datetime.min),
                    int(getattr(item, "id", 0) or 0),
                )
            )
        elif action == "delete":
            vital_id = payload.get("vital_id")
            if vital_id is not None:
                chart_vitals = [
                    vital
                    for vital in chart_vitals
                    if int(getattr(vital, "id", 0) or 0) != int(vital_id)
                ]

        if action in {"upsert", "delete"}:
            self._current_chart_vitals = chart_vitals
            self._current_operation_has_vitals = bool(
                payload.get("has_vitals", bool(chart_vitals))
            )
        self._apply_protocol_controls_state()
        if action in {"upsert", "delete"} and getattr(self, "_vitals_tab_built", False):
            QTimer.singleShot(0, self._update_vitals_chart)
        self.refresh_protocol(force=True)
        self.refresh_board(force=True)
        record_metric(
            "operblock_vitals_local_apply_ms",
            round((time.perf_counter() - started) * 1000.0, 3),
            operation_case_id=int(self._current_operation_case_id or 0),
            action=action or "unknown",
            ui_sync_reads=0,
        )
