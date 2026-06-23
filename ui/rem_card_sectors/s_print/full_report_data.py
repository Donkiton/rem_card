from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Iterable, Optional

from rem_card.services.shift_service import ShiftService
from .emergency_notice import attach_notice_for_period

from .movement import (
    build_changed_day_movement_struct,
    build_full_movement_struct,
    movement_summary_date as resolve_movement_summary_date,
)


DEFAULT_VITAL_SETTINGS = {"ad": 1, "pulse": 1, "temp": 1, "spo2": 1, "rr": 0, "cvp": 0}


def _as_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value).replace(" ", "T"))
    except Exception:
        return None


def _in_half_open_range(moment: Optional[datetime], start: datetime, end: datetime) -> bool:
    return bool(moment and start <= moment < end)


def _order_datetime(order: Any) -> Optional[datetime]:
    return _as_datetime(
        getattr(order, "_print_order_datetime", None)
        or getattr(order, "created_at", None)
    )


class _CachedStatusService:
    def __init__(self, original, outcome_context: Optional[dict[str, Any]]):
        self._original = original
        self._outcome_context = outcome_context

    def get_admission_outcome_context(self, admission_id: int) -> dict[str, Any]:
        if self._outcome_context is not None:
            return dict(self._outcome_context)
        if self._original and hasattr(self._original, "get_admission_outcome_context"):
            return self._original.get_admission_outcome_context(admission_id)
        return {}

    def __getattr__(self, name: str):
        return getattr(self._original, name)


class _CachedPrintService:
    def __init__(
        self,
        original,
        *,
        vital_settings_by_date: dict[str, dict[str, Any]],
        admin_rows: list[dict[str, Any]],
        oral_events: list[Any],
        diet_plans_by_shift: dict[str, Any],
        effective_bounds_by_shift: dict[str, tuple[datetime, datetime]],
        outcome_context: Optional[dict[str, Any]],
    ):
        self._original = original
        self._vital_settings_by_date = vital_settings_by_date
        self._admin_rows = admin_rows
        self._oral_events = oral_events
        self._diet_plans_by_shift = diet_plans_by_shift
        self._effective_bounds_by_shift = effective_bounds_by_shift
        self.status_service = _CachedStatusService(getattr(original, "status_service", None), outcome_context)

    def __getattr__(self, name: str):
        return getattr(self._original, name)

    def get_vital_settings_cached(self, admission_id: int, date: datetime) -> dict[str, Any]:
        date_key = date.strftime("%Y-%m-%d")
        return dict(self._vital_settings_by_date.get(date_key) or DEFAULT_VITAL_SETTINGS)

    def get_latest_administrations_for_order_ids(
        self,
        order_ids,
        start_dt: datetime,
        end_dt: datetime,
        **_kwargs,
    ) -> list[dict[str, Any]]:
        if not order_ids:
            return []
        order_id_set = {int(order_id) for order_id in order_ids if order_id is not None}
        rows = []
        for row in self._admin_rows:
            order_id = row.get("order_id")
            planned_time = _as_datetime(row.get("planned_time"))
            if order_id is None or int(order_id) not in order_id_set:
                continue
            if not _in_half_open_range(planned_time, start_dt, end_dt):
                continue
            rows.append(dict(row))
        return rows

    def _oral_context(self, shift_date: datetime):
        shift_start, _shift_end = ShiftService.get_day_period(shift_date)
        shift_key = shift_start.strftime("%Y-%m-%d %H:%M")
        start, end = self._effective_bounds_by_shift.get(shift_key, (shift_start, _shift_end))
        events = [
            event
            for event in self._oral_events
            if _in_half_open_range(getattr(event, "event_time", None), start, end)
        ]
        plan = self._diet_plans_by_shift.get(shift_key)
        return shift_start, start, end, events, plan

    def get_oral_intake_events(self, admission_id: int, shift_date: datetime) -> list[Any]:
        _shift_start, _start, _end, events, _plan = self._oral_context(shift_date)
        return list(events)

    def get_oral_intake_totals(self, admission_id: int, shift_date: datetime, current_time: Optional[datetime] = None) -> dict:
        shift_start, start, end, events, plan = self._oral_context(shift_date)
        calc_time = current_time or datetime.now()
        if calc_time < start:
            calc_time = start
        if calc_time >= end:
            calc_time = end
        oral_service = getattr(self._original, "_oral_intake", None)
        if oral_service and hasattr(oral_service, "_calculate_totals"):
            return oral_service._calculate_totals(events, plan, shift_start, start, end, calc_time.replace(second=0, microsecond=0))
        current = sum(float(getattr(event, "amount_ml", 0.0) or 0.0) for event in events if getattr(event, "event_time", start) <= calc_time)
        daily = sum(float(getattr(event, "amount_ml", 0.0) or 0.0) for event in events)
        return {"current": round(current, 1), "daily": round(daily, 1)}


class FullReportDataCollector:
    def __init__(
        self,
        remcard_service,
        admission_id: int,
        dates: Iterable[datetime],
        config: dict,
        transform_data: Callable[[dict, Any, dict], dict],
        *,
        include_ventilation: bool = False,
        unknown_patient_name: str = "Неизвестный пациент",
        unknown_icu_day: str = "?",
        missing_admission_icu_day: Optional[str] = None,
    ):
        self.remcard_service = remcard_service
        self.admission_id = int(admission_id)
        self.dates = list(dates or [])
        self.config = dict(config or {})
        self.transform_data = transform_data
        self.include_ventilation = bool(include_ventilation)
        self.unknown_patient_name = unknown_patient_name
        self.unknown_icu_day = unknown_icu_day
        self.missing_admission_icu_day = missing_admission_icu_day or unknown_icu_day
        self._plan_report_shift_start: Optional[datetime] = None

    def collect(self) -> list[dict]:
        periods = [(dt, *self.remcard_service.get_day_period(dt)) for dt in self.dates]
        if not periods:
            return []
        self._plan_report_shift_start = self._resolve_plan_report_shift_start()

        report_start = min(start for _dt, start, _end in periods)
        report_end = max(end for _dt, _start, end in periods)

        include_events = self.config.get("events", True)
        include_balance = self.config.get("balance", True)

        patient = self.remcard_service.get_patient(self.admission_id)
        current_status = self._get_current_status() if include_balance else None
        movement_events = self._get_movement_events() if include_events else []
        movement_struct = build_full_movement_struct(movement_events) if include_events else []
        movement_summary_date = (
            resolve_movement_summary_date(periods, movement_events)
            if include_events
            else None
        )

        vital_bounds_by_shift = self._build_vital_bounds(periods, patient)
        active_intervals_by_shift = self._build_active_intervals(periods)
        balance_bounds_by_shift = (
            self._build_balance_bounds(periods, patient, current_status)
            if include_balance
            else {}
        )
        all_vitals = self._get_all_vitals(report_start, report_end)
        all_orders = self._get_all_orders(report_start, report_end)
        all_admin_rows = self._get_all_admin_rows(all_orders, report_start, report_end)
        all_fluids = self._get_all_fluids(report_start, report_end)
        all_ventilation_events = self._get_all_ventilation_events(report_start, report_end)
        vital_settings_by_date = self._get_vital_settings_by_date(periods)
        oral_events = self._get_oral_events(report_start, report_end)
        diet_plans = self._get_diet_plans(periods)
        outcome_context = self._get_outcome_context()

        cached_service = _CachedPrintService(
            self.remcard_service,
            vital_settings_by_date=vital_settings_by_date,
            admin_rows=all_admin_rows,
            oral_events=oral_events,
            diet_plans_by_shift=diet_plans,
            effective_bounds_by_shift=vital_bounds_by_shift,
            outcome_context=outcome_context,
        )

        results = []
        for dt, start_dt, end_dt in periods:
            shift_key = start_dt.strftime("%Y-%m-%d %H:%M")
            day_data = self._base_day_data(patient, start_dt, end_dt)
            if self.config.get("vitals", True):
                day_data["vitals"] = self._filter_vitals(all_vitals, vital_bounds_by_shift[shift_key])
                day_data["vitals_active_intervals"] = active_intervals_by_shift.get(shift_key, [])

            day_data["prescriptions"] = self._filter_orders(all_orders, start_dt, end_dt)

            if self.config.get("balance", True):
                day_data["fluids_raw"] = self._filter_fluids(all_fluids, balance_bounds_by_shift[shift_key])

            if self.config.get("events", True):
                self._attach_movement(day_data, dt, start_dt, end_dt, movement_summary_date, movement_struct, movement_events)

            if self.include_ventilation and self.config.get("ventilation", False):
                day_data["ventilation_events"] = self._filter_ventilation_events(all_ventilation_events, start_dt, end_dt)

            results.append(self.transform_data(day_data, cached_service, self.config))
        return results

    def _resolve_plan_report_shift_start(self) -> Optional[datetime]:
        if not hasattr(self.remcard_service, "build_plan_card_state"):
            return None
        try:
            state = self.remcard_service.build_plan_card_state(self.admission_id)
        except Exception:
            return None
        if not state.get("plan_card_window_active") or not state.get("plan_card_exists"):
            return None
        target_date = state.get("plan_card_target_date")
        if not target_date:
            return None
        try:
            target_start, _target_end = self.remcard_service.get_day_period(target_date)
            return target_start
        except Exception:
            return None

    def _base_day_data(self, patient, start_dt: datetime, end_dt: datetime) -> dict:
        if patient:
            patient_name = f"{patient.last_name or ''} {patient.first_name or ''} {patient.middle_name or ''}".strip()
            diagnosis = getattr(patient, "diagnosis_text", None) or "—"
            if patient.admission_datetime:
                icu_day_value = ShiftService.calculate_icu_day(patient.admission_datetime, start_dt)
                icu_day = str(icu_day_value) if icu_day_value is not None else self.unknown_icu_day
            else:
                icu_day = self.missing_admission_icu_day
        else:
            patient_name = self.unknown_patient_name
            diagnosis = "—"
            icu_day = self.unknown_icu_day

        is_plan_card = bool(self._plan_report_shift_start and start_dt == self._plan_report_shift_start)
        data = {
            "admission_id": self.admission_id,
            "patient_name": patient_name,
            "diagnosis": diagnosis,
            "icu_day": icu_day,
            "start_dt": start_dt,
            "end_dt": end_dt,
            "is_plan_card": is_plan_card,
            "report_title": (
                "ПЛАНИРУЕМАЯ РЕАНИМАЦИОННАЯ КАРТА"
                if is_plan_card
                else "РЕАНИМАЦИОННАЯ КАРТА"
            ),
            "vitals": [],
            "prescriptions": [],
            "events": [],
            "fluids_raw": [],
        }
        attach_notice_for_period(data, patient, start_dt, end_dt)
        if self.include_ventilation:
            data["ventilation_events"] = []
        return data

    def _get_current_status(self):
        status_service = getattr(self.remcard_service, "status_service", None)
        if status_service and hasattr(status_service, "get_current_status"):
            try:
                return status_service.get_current_status(self.admission_id)
            except Exception:
                return None
        return None

    def _get_movement_events(self):
        status_service = getattr(self.remcard_service, "status_service", None)
        if status_service and hasattr(status_service, "get_events"):
            return status_service.get_events(self.admission_id)
        return []

    def _build_vital_bounds(self, periods, patient) -> dict[str, tuple[datetime, datetime]]:
        vital_service = getattr(self.remcard_service, "_vitals", None)
        bounds = {}
        for dt, start_dt, end_dt in periods:
            key = start_dt.strftime("%Y-%m-%d %H:%M")
            if vital_service and hasattr(vital_service, "get_effective_bounds_for_patient"):
                bounds[key] = vital_service.get_effective_bounds_for_patient(
                    patient,
                    dt,
                    default_bounds=(start_dt, end_dt),
                )
            else:
                bounds[key] = (start_dt, end_dt)
        return bounds

    def _build_active_intervals(self, periods) -> dict[str, list[tuple[datetime, datetime]]]:
        status_service = getattr(self.remcard_service, "status_service", None)
        intervals_by_shift = {}
        for _dt, start_dt, end_dt in periods:
            key = start_dt.strftime("%Y-%m-%d %H:%M")
            if status_service and hasattr(status_service, "get_active_intervals"):
                try:
                    intervals_by_shift[key] = status_service.get_active_intervals(
                        self.admission_id,
                        start_dt,
                        end_dt,
                    )
                except Exception:
                    intervals_by_shift[key] = []
            else:
                intervals_by_shift[key] = []
        return intervals_by_shift

    def _build_balance_bounds(self, periods, patient, current_status) -> dict[str, tuple[datetime, datetime]]:
        fluid_service = getattr(self.remcard_service, "fluid_service", None)
        bounds = {}
        for dt, start_dt, end_dt in periods:
            key = start_dt.strftime("%Y-%m-%d %H:%M")
            if fluid_service and hasattr(fluid_service, "get_balance_bounds_for_state"):
                bounds[key] = fluid_service.get_balance_bounds_for_state(
                    self.admission_id,
                    dt,
                    patient=patient,
                    current_status=current_status,
                    shift_bounds=(start_dt, end_dt),
                )
            else:
                bounds[key] = (start_dt, end_dt)
        return bounds

    def _get_all_vitals(self, report_start: datetime, report_end: datetime) -> list[Any]:
        if not self.config.get("vitals", True):
            return []
        dao = getattr(self.remcard_service, "vitals_dao", None)
        if dao and hasattr(dao, "get_vitals"):
            return dao.get_vitals(self.admission_id, report_start, report_end)
        return []

    def _get_all_orders(self, report_start: datetime, report_end: datetime) -> list[Any]:
        dao = getattr(self.remcard_service, "orders_dao", None)
        if dao and hasattr(dao, "get_orders_in_range"):
            return dao.get_orders_in_range(
                self.admission_id,
                report_start,
                report_end,
                only_committed=True,
            )
        orders = self.remcard_service.get_orders(self.admission_id, None, only_committed=True)
        self._attach_order_datetimes(orders)
        return orders

    def _attach_order_datetimes(self, orders: list[Any]) -> None:
        if not orders:
            return
        db = getattr(getattr(self.remcard_service, "orders_dao", None), "db", None)
        if db is None:
            return
        rows = db.fetch_all_remcard(
            "SELECT id, datetime FROM orders WHERE admission_id = ?",
            (self.admission_id,),
        )
        datetime_by_id = {
            int(row["id"]): _as_datetime(row["datetime"])
            for row in rows
            if row and row["id"] is not None
        }
        for order in orders:
            order_id = getattr(order, "id", None)
            if order_id is None:
                continue
            value = datetime_by_id.get(int(order_id))
            if value is not None:
                setattr(order, "_print_order_datetime", value)

    def _get_all_admin_rows(self, all_orders: list[Any], report_start: datetime, report_end: datetime) -> list[dict[str, Any]]:
        order_ids = [getattr(order, "id", None) for order in all_orders if getattr(order, "id", None) is not None]
        if not order_ids:
            return []
        rows = self.remcard_service.get_latest_administrations_for_order_ids(
            order_ids=order_ids,
            start_dt=report_start,
            end_dt=report_end,
            only_committed=True,
            include_deleted=False,
            include_cancelled=False,
            include_deleted_orders=True,
        )
        return [dict(row) for row in rows]

    def _get_all_fluids(self, report_start: datetime, report_end: datetime) -> list[Any]:
        if not self.config.get("balance", True):
            return []
        fluid_service = getattr(self.remcard_service, "fluid_service", None)
        if fluid_service and hasattr(fluid_service, "get_fluids_in_bounds"):
            return fluid_service.get_fluids_in_bounds(self.admission_id, report_start, report_end)
        return []

    def _get_all_ventilation_events(self, report_start: datetime, report_end: datetime) -> list[Any]:
        if not (self.include_ventilation and self.config.get("ventilation", False)):
            return []
        if not hasattr(self.remcard_service, "get_ventilation_timeline"):
            return []
        return self.remcard_service.get_ventilation_timeline(self.admission_id) or []

    def _get_vital_settings_by_date(self, periods) -> dict[str, dict[str, Any]]:
        date_keys = sorted({start_dt.strftime("%Y-%m-%d") for _dt, start_dt, _end in periods})
        if not date_keys:
            return {}
        db = getattr(getattr(self.remcard_service, "vitals_dao", None), "db", None)
        if db is None:
            return {date_key: dict(DEFAULT_VITAL_SETTINGS) for date_key in date_keys}
        rows = db.fetch_all_remcard(
            """
            SELECT date, ad, pulse, temp, spo2, rr, cvp
            FROM vital_settings
            WHERE admission_id = ? AND date <= ?
            ORDER BY date ASC
            """,
            (self.admission_id, max(date_keys)),
        )
        settings_rows = [dict(row) for row in rows]
        result = {}
        latest = dict(DEFAULT_VITAL_SETTINGS)
        row_index = 0
        for date_key in date_keys:
            while row_index < len(settings_rows) and str(settings_rows[row_index].get("date") or "") <= date_key:
                row = settings_rows[row_index]
                latest = {
                    "ad": int(row["ad"]) if row.get("ad") is not None else DEFAULT_VITAL_SETTINGS["ad"],
                    "pulse": int(row["pulse"]) if row.get("pulse") is not None else DEFAULT_VITAL_SETTINGS["pulse"],
                    "temp": int(row["temp"]) if row.get("temp") is not None else DEFAULT_VITAL_SETTINGS["temp"],
                    "spo2": int(row["spo2"]) if row.get("spo2") is not None else DEFAULT_VITAL_SETTINGS["spo2"],
                    "rr": int(row["rr"]) if row.get("rr") is not None else DEFAULT_VITAL_SETTINGS["rr"],
                    "cvp": int(row["cvp"]) if row.get("cvp") is not None else DEFAULT_VITAL_SETTINGS["cvp"],
                }
                row_index += 1
            result[date_key] = dict(latest)
        return result

    def _get_oral_events(self, report_start: datetime, report_end: datetime) -> list[Any]:
        if not self.config.get("balance", True):
            return []
        oral_service = getattr(self.remcard_service, "_oral_intake", None)
        dao = getattr(oral_service, "dao", None)
        if dao and hasattr(dao, "get_events"):
            return dao.get_events(self.admission_id, report_start, report_end)
        return []

    def _get_diet_plans(self, periods) -> dict[str, Any]:
        if not self.config.get("balance", True):
            return {}
        diet_service = getattr(self.remcard_service, "_diet_plan", None)
        dao = getattr(diet_service, "dao", None)
        db = getattr(dao, "db", None)
        if db is None:
            return {}
        shift_keys = [start_dt.strftime("%Y-%m-%d %H:%M") for _dt, start_dt, _end in periods]
        if not shift_keys:
            return {}
        placeholders = ",".join("?" for _ in shift_keys)
        rows = db.fetch_all_remcard(
            f"""
            SELECT *
            FROM diet_plan
            WHERE admission_id = ? AND shift_start IN ({placeholders})
            """,
            (self.admission_id, *shift_keys),
        )
        result = {}
        for row in rows:
            plan = dao._map(row) if hasattr(dao, "_map") else row
            result[getattr(plan, "shift_start", _as_datetime(row["shift_start"])).strftime("%Y-%m-%d %H:%M")] = plan
        return result

    def _get_outcome_context(self) -> Optional[dict[str, Any]]:
        status_service = getattr(self.remcard_service, "status_service", None)
        if status_service and hasattr(status_service, "get_admission_outcome_context"):
            try:
                return status_service.get_admission_outcome_context(self.admission_id) or {}
            except Exception:
                return {}
        return {}

    def _filter_vitals(self, all_vitals: list[Any], bounds: tuple[datetime, datetime]) -> list[Any]:
        start, end = bounds
        return [vital for vital in all_vitals if start <= getattr(vital, "timestamp", start) <= end]

    def _filter_orders(self, all_orders: list[Any], start_dt: datetime, end_dt: datetime) -> list[Any]:
        return [order for order in all_orders if _in_half_open_range(_order_datetime(order), start_dt, end_dt)]

    def _filter_fluids(self, all_fluids: list[Any], bounds: tuple[datetime, datetime]) -> list[Any]:
        start, end = bounds
        return [fluid for fluid in all_fluids if _in_half_open_range(getattr(fluid, "timestamp", None), start, end)]

    def _filter_ventilation_events(self, all_events: list[Any], start_dt: datetime, end_dt: datetime) -> list[Any]:
        return [event for event in all_events if _in_half_open_range(getattr(event, "timestamp", None), start_dt, end_dt)]

    def _attach_movement(
        self,
        day_data: dict,
        dt: datetime,
        start_dt: datetime,
        end_dt: datetime,
        movement_summary_date,
        movement_struct: list[dict[str, str]],
        movement_events: list[Any],
    ):
        if dt == movement_summary_date and movement_struct:
            day_data["events_struct_override"] = movement_struct
            return
        day_movement_struct = build_changed_day_movement_struct(movement_events, start_dt, end_dt)
        if day_movement_struct:
            day_data["events_struct_override"] = day_movement_struct
        else:
            day_data["events_struct_override"] = []
            day_data["hide_events_section"] = True


def collect_full_report_data(
    remcard_service,
    admission_id: int,
    dates: Iterable[datetime],
    config: dict,
    transform_data: Callable[[dict, Any, dict], dict],
    *,
    include_ventilation: bool = False,
    unknown_patient_name: str = "Неизвестный пациент",
    unknown_icu_day: str = "?",
    missing_admission_icu_day: Optional[str] = None,
) -> list[dict]:
    return FullReportDataCollector(
        remcard_service,
        admission_id,
        dates,
        config,
        transform_data,
        include_ventilation=include_ventilation,
        unknown_patient_name=unknown_patient_name,
        unknown_icu_day=unknown_icu_day,
        missing_admission_icu_day=missing_admission_icu_day,
    ).collect()
