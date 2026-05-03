from typing import List, Optional, Tuple, Dict, Any, Callable, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from PySide6.QtCore import QObject, Signal
from rem_card.app.logger import logger
from ..data.dao.sync_cursor import is_cursor_newer, normalize_sync_cursor
from ..data.dto.remcard_dto import (
    PatientDTO,
    VitalDTO,
    FluidDTO,
    DietTemplateDTO,
    DietPlanDTO,
    OralIntakeEventDTO,
    OrderDTO,
    OrderStatus,
    VentilationCaseDTO,
    VentilationEventDTO,
)
from .patient_service import PatientService
from .vital_service import VitalService
from .fluid_service import FluidService
from .diet_service import DietPlanService, DietTemplateService, OralIntakeService
from .order_service import OrderService
from .shift_service import ShiftService
from .ventilation_service import VentilationService

_ORDERS_SNAPSHOT_CALLER = ContextVar("remcard_orders_snapshot_caller", default="legacy")
_ORDERS_SNAPSHOT_CONTEXT_HASH = ContextVar("remcard_orders_snapshot_context_hash", default="unknown")
_DIRECT_ORDERS_BUILD_WARNED: set[tuple[str, int, str, str]] = set()
_LEGACY_ORDERS_ACCESS_COUNT = 0


@contextmanager
def orders_snapshot_caller(source: str, *, context_hash: Optional[str] = None):
    token = _ORDERS_SNAPSHOT_CALLER.set(str(source or "legacy"))
    context_token = _ORDERS_SNAPSHOT_CONTEXT_HASH.set(str(context_hash or "unknown"))
    try:
        yield
    finally:
        _ORDERS_SNAPSHOT_CONTEXT_HASH.reset(context_token)
        _ORDERS_SNAPSHOT_CALLER.reset(token)

class RemCardService(QObject):
    """Р¤Р°СЃР°Рґ, РѕР±СЉРµРґРёРЅСЏСЋС‰РёР№ РІСЃРµ РїРѕРґСЃРµСЂРІРёСЃС‹ РґР»СЏ СѓРґРѕР±СЃС‚РІР° РёСЃРїРѕР»СЊР·РѕРІР°РЅРёСЏ РІ UI."""

    @property
    def status_service(self):
        return getattr(self, "_status_service", None)

    @status_service.setter
    def status_service(self, value):
        self._status_service = value
        vitals_service = getattr(self, "_vitals", None)
        if vitals_service is not None:
            vitals_service.status_service = value

    @property
    def fluid_service(self):
        return self._fluids

    def __init__(self, vitals_dao, fluids_dao, orders_dao, ventilation_dao, patient_dao, status_service=None, data_service=None):
        super().__init__()
        # Р”Р»СЏ РѕР±СЂР°С‚РЅРѕР№ СЃРѕРІРјРµСЃС‚РёРјРѕСЃС‚Рё (СѓСЃС‚Р°СЂРµРІС€РµРµ, РїР»Р°РЅРёСЂСѓРµС‚СЃСЏ Рє СѓРґР°Р»РµРЅРёСЋ РїРѕСЃР»Рµ СЂРµС„Р°РєС‚РѕСЂРёРЅРіР° UI)
        self.vitals_dao = vitals_dao
        self.fluids_dao = fluids_dao
        self.orders_dao = orders_dao
        self.ventilation_dao = ventilation_dao
        self.patient_dao = patient_dao
        self.status_service = status_service
        self.data_service = data_service

        # РРЅРёС†РёР°Р»РёР·Р°С†РёСЏ СЃРїРµС†РёР°Р»РёР·РёСЂРѕРІР°РЅРЅС‹С… СЃРµСЂРІРёСЃРѕРІ
        self._patients = PatientService(patient_dao)
        self._vitals = VitalService(vitals_dao, patient_dao, status_service)
        self._fluids = FluidService(fluids_dao, self._vitals, data_service=data_service)
        self._orders = OrderService(orders_dao)
        self._ventilation = VentilationService(ventilation_dao, data_service=data_service) if ventilation_dao else None
        self._shifts = ShiftService()

        from rem_card.data.dao.diet_dao import DietPlanDAO, OralIntakeDAO

        self.diet_plan_dao = DietPlanDAO(self.orders_dao.db)
        self.oral_intake_dao = OralIntakeDAO(self.orders_dao.db)
        self._diet_templates = DietTemplateService()
        self._diet_plan = DietPlanService(self.diet_plan_dao, self._diet_templates)
        self._oral_intake = OralIntakeService(self.oral_intake_dao, self._vitals, self._diet_plan)

    def has_card(self, admission_id: int, date: datetime) -> bool:
        """
        РџСЂРѕРІРµСЂСЏРµС‚ СЃСѓС‰РµСЃС‚РІРѕРІР°РЅРёРµ РєР°СЂС‚С‹ Р·Р° СѓРєР°Р·Р°РЅРЅСѓСЋ РґР°С‚Сѓ.
        РљР°СЂС‚Р° СЃС‡РёС‚Р°РµС‚СЃСЏ СЃСѓС‰РµСЃС‚РІСѓСЋС‰РµР№, РµСЃР»Рё РІ Р‘Р” РµСЃС‚СЊ С…РѕС‚СЊ РѕРґРЅР° Р·Р°РїРёСЃСЊ Р·Р° СЌС‚РѕС‚ РїРµСЂРёРѕРґ,
        РґР°Р¶Рµ РµСЃР»Рё РѕРЅР° РїРѕРјРµС‡РµРЅР° РєР°Рє 'deleted' (РЅРѕ СЏРІР»СЏРµС‚СЃСЏ СЃРѕС…СЂР°РЅРµРЅРЅРѕР№/СЃСѓС‰РµСЃС‚РІСѓСЋС‰РµР№ РІ Р‘Р”).
        """
        start, end = self.get_day_period(date)
        
        # РџСЂРѕРІРµСЂРєР° РІРёС‚Р°Р»СЊРЅС‹С… С„СѓРЅРєС†РёР№ (РѕРЅРё СѓРґР°Р»СЏСЋС‚СЃСЏ С„РёР·РёС‡РµСЃРєРё, С‚Р°Рє С‡С‚Рѕ len > 0 РґРѕСЃС‚Р°С‚РѕС‡РЅРѕ)
        vitals = self.get_vitals(admission_id, date)
        if len(vitals) > 0:
            return True
            
        # РџСЂРѕРІРµСЂРєР° Р¶РёРґРєРѕСЃС‚РµР№ (С‚Р°РєР¶Рµ СѓРґР°Р»СЏСЋС‚СЃСЏ С„РёР·РёС‡РµСЃРєРё)
        fluids = self.get_fluids(admission_id, date)
        if len(fluids) > 0:
            return True

        diet_row = self.orders_dao.db.fetch_one_remcard(
            """
            SELECT EXISTS (
                SELECT 1 FROM diet_plan
                WHERE admission_id = ?
                  AND shift_start >= ? AND shift_start < ?
            )
            OR EXISTS (
                SELECT 1 FROM oral_intake_events
                WHERE admission_id = ?
                  AND event_time >= ? AND event_time < ?
            )
            """,
            (
                admission_id,
                start.isoformat(timespec="minutes").replace("T", " "),
                end.isoformat(timespec="minutes").replace("T", " "),
                admission_id,
                start.isoformat(timespec="minutes").replace("T", " "),
                end.isoformat(timespec="minutes").replace("T", " "),
            ),
        )
        if diet_row and bool(diet_row[0]):
            return True
            
        # РџСЂРѕРІРµСЂРєР° РЅР°Р·РЅР°С‡РµРЅРёР№
        # Р—РґРµСЃСЊ РєСЂРёС‚РёС‡РЅРѕ: РµСЃР»Рё РјС‹ СЃРґРµР»Р°Р»Рё РЎРљРњ (status='deleted', is_committed=0), 
        # С‚Рѕ Р·Р°РїРёСЃСЊ РІСЃРµ РµС‰Рµ СЃСѓС‰РµСЃС‚РІСѓРµС‚ РІ Р‘Р” Рё РґРѕР»Р¶РЅР° СѓРґРµСЂР¶РёРІР°С‚СЊ СЃС‚Р°С‚СѓСЃ "РєР°СЂС‚Р° СЃСѓС‰РµСЃС‚РІСѓРµС‚".
        # РСЃРїРѕР»СЊР·СѓРµРј РїСЂСЏРјРѕР№ Р·Р°РїСЂРѕСЃ Рє DAO РґР»СЏ РїСЂРѕРІРµСЂРєРё С„РёР·РёС‡РµСЃРєРѕРіРѕ РЅР°Р»РёС‡РёСЏ Р»СЋР±С‹С… Р·Р°РїРёСЃРµР№ Р·Р° РїРµСЂРёРѕРґ.
        query = """
            SELECT EXISTS (
                SELECT 1 FROM orders 
                WHERE admission_id = ? 
                AND datetime >= ? AND datetime < ?
            )
        """
        res = self.orders_dao.db.fetch_one_remcard(query, (admission_id, start.isoformat(), end.isoformat()))
        return bool(res[0]) if res else False

    def has_cards_bulk(self, admission_ids: Sequence[int], date: datetime) -> Dict[int, bool]:
        ids = [int(adm_id) for adm_id in admission_ids if adm_id is not None]
        if not ids:
            return {}

        start, end = self.get_day_period(date)
        placeholders = ",".join(["(?)"] * len(ids))
        query = f"""
            WITH ids(admission_id) AS (VALUES {placeholders})
            SELECT
                ids.admission_id AS admission_id,
                CASE
                    WHEN EXISTS (
                        SELECT 1 FROM vitals v
                        WHERE v.admission_id = ids.admission_id
                          AND v.datetime >= ? AND v.datetime < ?
                    )
                    OR EXISTS (
                        SELECT 1 FROM fluids f
                        WHERE f.admission_id = ids.admission_id
                          AND f.datetime >= ? AND f.datetime < ?
                    )
                    OR EXISTS (
                        SELECT 1 FROM orders o
                        WHERE o.admission_id = ids.admission_id
                          AND o.datetime >= ? AND o.datetime < ?
                    )
                    OR EXISTS (
                        SELECT 1 FROM diet_plan dp
                        WHERE dp.admission_id = ids.admission_id
                          AND dp.shift_start >= ? AND dp.shift_start < ?
                    )
                    OR EXISTS (
                        SELECT 1 FROM oral_intake_events oi
                        WHERE oi.admission_id = ids.admission_id
                          AND oi.event_time >= ? AND oi.event_time < ?
                    )
                    THEN 1
                    ELSE 0
                END AS has_card
            FROM ids
        """
        params = tuple(ids) + (
            start.isoformat(),
            end.isoformat(),
            start.isoformat(),
            end.isoformat(),
            start.isoformat(),
            end.isoformat(),
            start.isoformat(timespec="minutes").replace("T", " "),
            end.isoformat(timespec="minutes").replace("T", " "),
            start.isoformat(timespec="minutes").replace("T", " "),
            end.isoformat(timespec="minutes").replace("T", " "),
        )
        rows = self.orders_dao.db.fetch_all_remcard(query, params)
        result: Dict[int, bool] = {adm_id: False for adm_id in ids}
        for row in rows:
            result[int(row["admission_id"])] = bool(row["has_card"])
        return result

    def get_beds_runtime_snapshot(
        self,
        admission_ids: Sequence[int],
        now: datetime,
        yesterday: datetime,
    ) -> Dict[int, Dict[str, Any]]:
        ids = [int(adm_id) for adm_id in admission_ids if adm_id is not None]
        if not ids:
            return {}

        status_map: Dict[int, Any] = {adm_id: None for adm_id in ids}
        if self.status_service and hasattr(self.status_service, "get_current_statuses_bulk"):
            try:
                status_map.update(self.status_service.get_current_statuses_bulk(ids))
            except Exception:
                for adm_id in ids:
                    status_map[adm_id] = self.status_service.get_current_status(adm_id)

        card_now_map = self.has_cards_bulk(ids, now)
        card_yest_map = self.has_cards_bulk(ids, yesterday)
        latest_values_map = self._vitals.get_latest_vital_values_bulk(ids)
        settings_map = self._vitals.get_vital_settings_cached_bulk(ids, now)

        default_values = {
            "sys": None,
            "dia": None,
            "pulse": None,
            "temp": None,
            "spo2": None,
            "rr": None,
            "cvp": None,
        }
        default_settings = {"ad": 1, "pulse": 1, "temp": 1, "spo2": 1, "rr": 0, "cvp": 0}

        snapshot: Dict[int, Dict[str, Any]] = {}
        for adm_id in ids:
            snapshot[adm_id] = {
                "status": status_map.get(adm_id),
                "card_exists": bool(card_now_map.get(adm_id, False)),
                "yest_exists": bool(card_yest_map.get(adm_id, False)),
                "latest_values": dict(latest_values_map.get(adm_id, default_values)),
                "settings": dict(settings_map.get(adm_id, default_settings)),
            }
        return snapshot

    def build_beds_snapshot(
        self,
        reference_dt: Optional[datetime] = None,
        *,
        include_change_cursor: bool = False,
    ) -> Dict[str, Any]:
        now = reference_dt or datetime.now()
        yesterday = now - timedelta(days=1)
        active_patients = self.get_active_patients()
        ordered_ids = [
            int(getattr(patient, "id"))
            for patient in active_patients
            if getattr(patient, "id", None) is not None
        ]
        runtime_snapshot = self.get_beds_runtime_snapshot(ordered_ids, now, yesterday) if ordered_ids else {}
        snapshot = {
            "patients": active_patients,
            "now": now,
            "yesterday": yesterday,
            "runtime_snapshot": runtime_snapshot,
        }
        if include_change_cursor:
            snapshot["change_id"] = self.get_latest_change_id(admission_id=None, include_global=True)
        return snapshot

    def get_current_status(self, admission_id: int):
        if not self.status_service:
            return None
        return self.status_service.get_current_status(admission_id)

    def build_vitals_snapshot(
        self,
        admission_id: int,
        date: datetime,
        *,
        include_change_cursor: bool = False,
        ensure_initial_status: bool = False,
    ) -> Dict[str, Any]:
        start_dt, end_dt = self.get_day_period(date)
        patient = self.get_patient(admission_id)

        if ensure_initial_status and self.status_service:
            admission_dt = getattr(patient, "admission_datetime", None) if patient else None
            self.status_service.ensure_initial_status(admission_id, start_dt, admission_dt)

        current_status = self.get_current_status(admission_id)
        effective_bounds = self._vitals.get_effective_bounds_for_patient(
            patient,
            date,
            default_bounds=(start_dt, end_dt),
        )
        chart_start_dt, chart_end_dt = self._vitals.get_chart_window_bounds(date)
        vitals_extended = self.vitals_dao.get_vitals(admission_id, chart_start_dt, chart_end_dt)
        effective_start, effective_end = effective_bounds
        vitals = [
            vital
            for vital in vitals_extended
            if effective_start <= vital.timestamp <= effective_end
        ]
        latest_values = self.get_latest_vital_values(admission_id)
        settings = self.get_vital_settings_cached(admission_id, date)
        active_intervals = (
            self.status_service.get_active_intervals(admission_id, chart_start_dt, chart_end_dt)
            if self.status_service and hasattr(self.status_service, "get_active_intervals")
            else []
        )
        yest_date = date - timedelta(days=1)
        card_exists = True if vitals else self.has_card(admission_id, date)

        snapshot: Dict[str, Any] = {
            "admission_id": admission_id,
            "shift_date": date,
            "start_dt": start_dt,
            "end_dt": end_dt,
            "patient": patient,
            "status": current_status,
            "vitals": vitals,
            "vitals_extended": vitals_extended,
            "latest_values": latest_values,
            "settings": settings,
            "effective_bounds": effective_bounds,
            "chart_active_intervals": active_intervals,
            "card_exists": card_exists,
            "yest_exists": self.has_card(admission_id, yest_date),
            "has_vitals": bool(vitals),
        }
        if include_change_cursor:
            snapshot["change_id"] = self.get_latest_change_id(admission_id)
        return snapshot

    def build_patient_header_snapshot(
        self,
        admission_id: int,
        date: datetime,
        *,
        include_change_cursor: bool = False,
    ) -> Dict[str, Any]:
        start_dt, end_dt = self.get_day_period(date)
        patient = self.get_patient(admission_id)
        current_status = self.get_current_status(admission_id)
        yest_date = date - timedelta(days=1)
        snapshot: Dict[str, Any] = {
            "admission_id": admission_id,
            "shift_date": date,
            "start_dt": start_dt,
            "end_dt": end_dt,
            "patient": patient,
            "status": current_status,
            "latest_values": self.get_latest_vital_values(admission_id),
            "settings": self.get_vital_settings_cached(admission_id, date),
            "card_exists": self.has_card(admission_id, date),
            "yest_exists": self.has_card(admission_id, yest_date),
        }
        if include_change_cursor:
            snapshot["change_id"] = self.get_latest_change_id(admission_id)
        return snapshot

    def build_status_snapshot(
        self,
        admission_id: int,
        date: datetime,
        *,
        include_change_cursor: bool = False,
    ) -> Dict[str, Any]:
        start_dt, end_dt = self._vitals.get_chart_window_bounds(date)
        snapshot: Dict[str, Any] = {
            "admission_id": admission_id,
            "shift_date": date,
            "status": self.get_current_status(admission_id),
            "active_intervals": (
                self.status_service.get_active_intervals(admission_id, start_dt, end_dt)
                if self.status_service and hasattr(self.status_service, "get_active_intervals")
                else []
            ),
        }
        if include_change_cursor:
            snapshot["change_id"] = self.get_latest_change_id(admission_id)
        return snapshot

    def build_balance_snapshot(
        self,
        admission_id: int,
        date: datetime,
        *,
        include_change_cursor: bool = False,
        balance_only_committed: bool = False,
    ) -> Dict[str, Any]:
        start_dt, end_dt = self.get_day_period(date)
        patient = self.get_patient(admission_id)
        current_status = self.get_current_status(admission_id)
        snapshot = self._build_balance_snapshot(
            admission_id=admission_id,
            shift_date=date,
            patient=patient,
            current_status=current_status,
            only_committed=balance_only_committed,
            start_dt=start_dt,
            end_dt=end_dt,
        )
        snapshot.update(
            {
                "admission_id": admission_id,
                "shift_date": date,
                "start_dt": start_dt,
                "end_dt": end_dt,
                "patient": patient,
                "status": current_status,
            }
        )
        if include_change_cursor:
            snapshot["change_id"] = self.get_latest_change_id(admission_id)
        return snapshot

    def build_diet_snapshot(
        self,
        admission_id: int,
        date: datetime,
        *,
        include_change_cursor: bool = False,
    ) -> Dict[str, Any]:
        snapshot: Dict[str, Any] = {
            "admission_id": admission_id,
            "shift_date": date,
            "plan": self.get_diet_plan(admission_id, date),
            "events": self.get_oral_intake_events(admission_id, date),
            "totals": self.get_oral_intake_totals(admission_id, date),
        }
        if include_change_cursor:
            snapshot["change_id"] = self.get_latest_change_id(admission_id)
        return snapshot

    def build_ivl_snapshot(
        self,
        admission_id: int,
        date: datetime,
        *,
        include_change_cursor: bool = False,
    ) -> Dict[str, Any]:
        summary = self.get_ventilation_summary(admission_id)
        snapshot: Dict[str, Any] = {
            "admission_id": admission_id,
            "shift_date": date,
            "summary": summary,
            "timeline": self.get_ventilation_timeline(admission_id),
            "latest_case": (
                self.get_latest_ventilation_case(admission_id)
                if not summary.get("active_case")
                else None
            ),
        }
        if include_change_cursor:
            snapshot["change_id"] = self.get_latest_change_id(admission_id)
        return snapshot

    def build_full_card_snapshot(
        self,
        admission_id: int,
        date: datetime,
        *,
        include_change_cursor: bool = False,
        include_balance: bool = True,
        balance_only_committed: bool = False,
        ensure_initial_status: bool = False,
    ) -> Dict[str, Any]:
        """Build a full card snapshot from vitals plus optional balance data."""
        snapshot = self.build_vitals_snapshot(
            admission_id,
            date,
            include_change_cursor=False,
            ensure_initial_status=ensure_initial_status,
        )
        patient = snapshot.get("patient")
        current_status = snapshot.get("status")
        start_dt = snapshot.get("start_dt")
        end_dt = snapshot.get("end_dt")

        if include_balance:
            snapshot.update(
                self._build_balance_snapshot(
                    admission_id=admission_id,
                    shift_date=date,
                    patient=patient,
                    current_status=current_status,
                    only_committed=balance_only_committed,
                    start_dt=start_dt,
                    end_dt=end_dt,
                )
            )

        if include_change_cursor:
            snapshot["change_id"] = self.get_latest_change_id(admission_id)

        return snapshot

    def build_card_snapshot(
        self,
        admission_id: int,
        date: datetime,
        *,
        include_change_cursor: bool = False,
        include_balance: bool = True,
        balance_only_committed: bool = False,
        ensure_initial_status: bool = False,
    ) -> Dict[str, Any]:
        """Compatibility wrapper. Prefer build_full_card_snapshot()."""
        return self.build_full_card_snapshot(
            admission_id,
            date,
            include_change_cursor=include_change_cursor,
            include_balance=include_balance,
            balance_only_committed=balance_only_committed,
            ensure_initial_status=ensure_initial_status,
        )

    def build_orders_snapshot(
        self,
        admission_id: int,
        shift_date: datetime,
        *,
        only_committed: bool = False,
        include_change_cursor: bool = False,
    ) -> Dict[str, Any]:
        global _LEGACY_ORDERS_ACCESS_COUNT
        caller = str(_ORDERS_SNAPSHOT_CALLER.get() or "legacy")
        context_hash = str(_ORDERS_SNAPSHOT_CONTEXT_HASH.get() or "unknown")
        if caller != "read_coordinator":
            _LEGACY_ORDERS_ACCESS_COUNT += 1
            warn_key = (caller, int(admission_id), shift_date.isoformat(), context_hash)
            if warn_key not in _DIRECT_ORDERS_BUILD_WARNED:
                _DIRECT_ORDERS_BUILD_WARNED.add(warn_key)
                logger.warning(
                    "[RemCardService] direct_orders_snapshot_build caller=%s admission_id=%s shift_date=%s only_committed=%s context_hash=%s legacy_orders_access_count=%s",
                    caller,
                    admission_id,
                    shift_date.isoformat(),
                    bool(only_committed),
                    context_hash,
                    _LEGACY_ORDERS_ACCESS_COUNT,
                )
        all_orders = self.get_orders(admission_id, shift_date, only_committed=only_committed)
        visible_orders = (
            [order for order in all_orders if order]
            if only_committed
            else [order for order in all_orders if order and order.status != OrderStatus.DELETED]
        )
        admin_rows = [
            dict(row)
            for row in self.get_latest_administrations(
                admission_id=admission_id,
                shift_date=shift_date,
                only_committed=only_committed,
                include_deleted=True,
                include_cancelled=True,
                include_deleted_orders=True,
            )
        ]

        snapshot: Dict[str, Any] = {
            "admission_id": admission_id,
            "shift_date": shift_date,
            "only_committed": bool(only_committed),
            "orders": visible_orders,
            "admin_rows": admin_rows,
            "has_any_draft": self._orders.has_drafts(admission_id, shift_date=shift_date),
            "has_any_administrations": any(
                str(row.get("status") or "") not in ("deleted", "cancelled")
                for row in admin_rows
            ),
            "has_any_orders": bool(visible_orders),
        }
        if include_change_cursor:
            snapshot["change_id"] = self.get_latest_change_id(admission_id, include_global=False)
        return snapshot

    def _build_balance_snapshot(
        self,
        *,
        admission_id: int,
        shift_date: datetime,
        patient: Optional[PatientDTO],
        current_status,
        only_committed: bool,
        start_dt: datetime,
        end_dt: datetime,
    ) -> Dict[str, Any]:
        from ..data.dto.remcard_dto import AdministrationDTO
        from .balance_calculator import BalanceCalculator

        if hasattr(self._fluids, "get_balance_bounds"):
            effective_start, effective_end = self._fluids.get_balance_bounds(admission_id, shift_date)
        else:
            effective_start, effective_end = self.get_effective_bounds(admission_id, shift_date)
        fluids = self.get_fluids(admission_id, shift_date)
        orders = self.get_orders(admission_id, shift_date, only_committed=only_committed)

        admin_rows = self.get_latest_administrations(
            admission_id=admission_id,
            shift_date=shift_date,
            only_committed=only_committed,
            include_deleted=False,
            include_cancelled=False,
            include_deleted_orders=True,
        )
        admin_map: Dict[int, list[AdministrationDTO]] = {}
        for row in admin_rows:
            mapped = dict(row)
            order_id = mapped["order_id"]
            admin_map.setdefault(order_id, []).append(
                AdministrationDTO(
                    id=mapped["id"],
                    order_id=order_id,
                    chain_id=mapped.get("chain_id"),
                    big_chain_id=mapped.get("big_chain_id"),
                    cell_role=mapped["cell_role"],
                    planned_time=datetime.fromisoformat(str(mapped["planned_time"]).replace(" ", "T")),
                    actual_time=(
                        datetime.fromisoformat(str(mapped["actual_time"]).replace(" ", "T"))
                        if mapped.get("actual_time")
                        else None
                    ),
                    performer_id=mapped.get("performer_id"),
                    status=mapped["status"],
                    is_committed=mapped.get("is_committed", 0),
                    comment=mapped.get("comment", ""),
                    volume_ml=mapped.get("volume_ml", 0.0),
                    updated_at=mapped.get("updated_at"),
                    last_modified_by=mapped.get("last_modified_by"),
                )
            )
        for order in orders:
            order.administrations = admin_map.get(order.id, [])

        active_intervals = (
            self.status_service.get_active_intervals(admission_id, start_dt, end_dt)
            if self.status_service and hasattr(self.status_service, "get_active_intervals")
            else []
        )

        outcome_time = None
        if current_status and current_status.status.is_outcome():
            outcome_time = current_status.start_time
        terminal_transfer_time = getattr(patient, "transfer_datetime", None) if patient else None
        if current_status and not current_status.status.is_outcome():
            terminal_transfer_time = None

        now = datetime.now()
        calc_time = now if start_dt <= now < end_dt else end_dt
        oral_events = []
        oral_totals = {"current": 0.0, "daily": 0.0}
        try:
            loaded_oral_events = self.get_oral_intake_events(admission_id, shift_date)
            oral_current = 0.0
            oral_daily = 0.0
            for event in loaded_oral_events:
                amount = float(getattr(event, "amount_ml", 0.0) or 0.0)
                event_time = getattr(event, "event_time", None)
                oral_events.append(
                    {
                        "event_time": event_time.isoformat() if event_time is not None else None,
                        "amount_ml": amount,
                    }
                )
                oral_daily += amount
                if event_time is not None and event_time <= calc_time:
                    oral_current += amount
            oral_totals = {"current": round(oral_current, 1), "daily": round(oral_daily, 1)}
        except Exception as exc:
            logger.warning("Failed to load oral intake runtime for balance snapshot: %s", exc)
        balance_calc = BalanceCalculator.calculate(
            orders=orders,
            current_time=calc_time,
            end_of_card=end_dt,
            transfer_time=terminal_transfer_time,
            active_intervals=active_intervals,
            outcome_time=outcome_time,
        )

        return {
            "effective_bounds": (effective_start, effective_end),
            "fluids": fluids,
            "balance_runtime": {
                "orders": orders,
                "start_dt": start_dt,
                "end_dt": end_dt,
                "transfer_time": terminal_transfer_time,
                "active_intervals": active_intervals,
                "outcome_time": outcome_time,
                "oral_events": oral_events,
                "oral_totals": oral_totals,
            },
            "balance_calc": balance_calc,
        }

    # --- Patient Service Methods ---
    def get_active_patients(self) -> List[PatientDTO]:
        return self._patients.get_active_patients()

    def maybe_release_due_outcome_beds(self, force: bool = False) -> int:
        return self._patients.maybe_release_due_outcome_beds(force=force)

    def get_outcome_bed_release_delay_minutes(self) -> int:
        return int(getattr(self._patients, "outcome_release_delay_minutes", 30))

    def get_archived_patients(self) -> List[PatientDTO]:
        return self._patients.get_archived_patients()

    def get_patient(self, admission_id: int) -> Optional[PatientDTO]:
        return self._patients.get_patient(admission_id)

    def delete_patient(self, patient_id: int):
        self._patients.delete_patient(patient_id)

    def delete_admission(self, admission_id: int):
        self._patients.delete_admission(admission_id)

    def sync_patients(self):
        self._patients.sync_patients()

    # --- Vital Service Methods ---
    def get_vital_settings_cached(self, admission_id: int, date: datetime) -> Dict[str, Any]:
        return self._vitals.get_vital_settings_cached(admission_id, date)

    def save_vital_settings(self, admission_id: int, date: datetime, settings: Dict[str, Any]):
        self._vitals.save_vital_settings(admission_id, date, settings)

    def get_vitals(self, admission_id: int, date: datetime) -> List[VitalDTO]:
        return self._vitals.get_vitals(admission_id, date)

    def get_vitals_extended(self, admission_id: int, date: datetime) -> List[VitalDTO]:
        return self._vitals.get_vitals_extended(admission_id, date)

    def add_vital(
        self,
        dto: VitalDTO,
        shift_date: Optional[datetime] = None,
        force: bool = False,
        expected_revision: Optional[int] = None,
    ):
        self._vitals.add_vital(dto, shift_date, force, expected_revision=expected_revision)

    def get_latest_vital(self, admission_id: int) -> Optional[VitalDTO]:
        return self._vitals.get_latest_vital(admission_id)

    def get_latest_vital_values(self, admission_id: int) -> Dict[str, Any]:
        return self._vitals.get_latest_vital_values(admission_id)

    def get_latest_vital_values_bulk(self, admission_ids: Sequence[int]) -> Dict[int, Dict[str, Any]]:
        return self._vitals.get_latest_vital_values_bulk(admission_ids)

    def get_vital_settings_cached_bulk(self, admission_ids: Sequence[int], date: datetime) -> Dict[int, Dict[str, Any]]:
        return self._vitals.get_vital_settings_cached_bulk(admission_ids, date)

    def clear_vitals(self, admission_id: int, date: datetime):
        self._vitals.clear_vitals(admission_id, date)

    def delete_last_vital(self, admission_id: int, date: datetime, expected_revision: Optional[int] = None):
        self._vitals.delete_last_vital(admission_id, date, expected_revision=expected_revision)

    def get_all_card_dates(self, admission_id: int) -> List[datetime]:
        """
        РЎРѕР±РёСЂР°РµС‚ РІСЃРµ РґР°С‚С‹, РєРѕРіРґР° Р±С‹Р»Рё РєР°РєРёРµ-Р»РёР±Рѕ Р·Р°РїРёСЃРё РїРѕ РїР°С†РёРµРЅС‚Сѓ (РІРёС‚Р°Р»С‹, Р¶РёРґРєРѕСЃС‚Рё РёР»Рё РЅР°Р·РЅР°С‡РµРЅРёСЏ).
        Р—Р°С‚РµРј РіСЂСѓРїРїРёСЂСѓРµС‚ РёС… РїРѕ РјРµРґРёС†РёРЅСЃРєРёРј СЃСѓС‚РєР°Рј С‡РµСЂРµР· ShiftService.
        """
        v_dates = self.vitals_dao.get_all_vital_dates(admission_id)
        o_dates = self.orders_dao.get_all_dates(admission_id)
        f_dates = self.fluids_dao.get_all_dates(admission_id)
        diet_dates = self._get_diet_raw_dates(admission_id)
        
        # РћР±СЉРµРґРёРЅСЏРµРј РІСЃРµ "СЃС‹СЂС‹Рµ" РґР°С‚С‹ РІ РѕРґРёРЅ СЃРїРёСЃРѕРє
        raw_dates = list(set(v_dates + o_dates + f_dates + diet_dates))
        
        return self._shifts.get_all_card_dates(raw_dates)

    def get_latest_card_date(self, admission_id: int) -> Optional[datetime]:
        """Р’РѕР·РІСЂР°С‰Р°РµС‚ РґР°С‚Сѓ РїРѕСЃР»РµРґРЅРµР№ Р·Р°РїРёСЃРё РґР»СЏ РїР°С†РёРµРЅС‚Р° (РїРѕ РІРёС‚Р°Р»Р°Рј, Р¶РёРґРєРѕСЃС‚СЏРј РёР»Рё РЅР°Р·РЅР°С‡РµРЅРёСЏРј)."""
        v_dates = self.vitals_dao.get_all_vital_dates(admission_id)
        o_dates = self.orders_dao.get_all_dates(admission_id)
        f_dates = self.fluids_dao.get_all_dates(admission_id)
        diet_dates = self._get_diet_raw_dates(admission_id)
        
        all_dates = v_dates + o_dates + f_dates + diet_dates
        return max(all_dates) if all_dates else None

    def _get_diet_raw_dates(self, admission_id: int) -> List[datetime]:
        rows = self.orders_dao.db.fetch_all_remcard(
            """
            SELECT shift_start AS dt FROM diet_plan WHERE admission_id = ?
            UNION ALL
            SELECT event_time AS dt FROM oral_intake_events WHERE admission_id = ?
            """,
            (int(admission_id), int(admission_id)),
        )
        dates: List[datetime] = []
        for row in rows:
            text = str(row["dt"] or "").strip()
            if not text:
                continue
            try:
                dates.append(datetime.fromisoformat(text.replace(" ", "T")))
            except Exception:
                continue
        return dates

    def is_timestamp_valid(self, admission_id: int, dt: datetime) -> Tuple[bool, str]:
        """РџСЂРѕРІРµСЂСЏРµС‚, РІР°Р»РёРґРЅР° Р»Рё РґР°С‚Р° РґР»СЏ СЃРѕР·РґР°РЅРёСЏ РєР°СЂС‚С‹."""
        patient = self.get_patient(admission_id)
        if not patient:
            return False, "РџР°С†РёРµРЅС‚ РЅРµ РЅР°Р№РґРµРЅ"
            
        # РџСЂРѕРІРµСЂРєР° РЅР° РґР°С‚Сѓ РїРѕСЃС‚СѓРїР»РµРЅРёСЏ
        if patient.admission_datetime and dt < patient.admission_datetime:
            adm_str = patient.admission_datetime.strftime("%d.%m.%Y %H:%M")
            return False, f"Р”Р°С‚Р° РєР°СЂС‚С‹ ({dt.strftime('%d.%m.%Y %H:%M')}) РЅРµ РјРѕР¶РµС‚ Р±С‹С‚СЊ СЂР°РЅСЊС€Рµ РґР°С‚С‹ РїРѕСЃС‚СѓРїР»РµРЅРёСЏ ({adm_str})"
            
        return True, "OK"

    # --- Fluid Service Methods ---
    def get_fluids(self, admission_id: int, date: datetime) -> List[FluidDTO]:
        return self._fluids.get_fluids(admission_id, date)

    def add_fluid(self, dto: FluidDTO, shift_date: Optional[datetime] = None):
        self._fluids.add_fluid(dto, shift_date)

    # --- Diet / Oral Intake Service Methods ---
    def list_diet_templates(self) -> List[DietTemplateDTO]:
        return self._diet_templates.list_templates()

    def get_diet_template(self, template_id: int) -> DietTemplateDTO:
        return self._diet_templates.get_template(template_id)

    def create_diet_template(self, name: str, diet_text: str = "", schedule_json=None, is_default: bool = False):
        return self._diet_templates.create_template(
            name=name,
            diet_text=diet_text,
            schedule_json=schedule_json,
            is_default=is_default,
        )

    def update_diet_template(
        self,
        template_id: int,
        name: str,
        diet_text: str = "",
        schedule_json=None,
        is_default: bool = False,
        expected_version: Optional[int] = None,
    ):
        return self._diet_templates.update_template(
            template_id=template_id,
            name=name,
            diet_text=diet_text,
            schedule_json=schedule_json,
            is_default=is_default,
            expected_version=expected_version,
        )

    def delete_diet_template(self, template_id: int, expected_version: Optional[int] = None):
        return self._diet_templates.delete_template(template_id, expected_version=expected_version)

    def get_diet_plan(self, admission_id: int, shift_date: datetime) -> Optional[DietPlanDTO]:
        return self._diet_plan.get_plan(admission_id, shift_date)

    def apply_diet_template(
        self,
        admission_id: int,
        shift_date: datetime,
        template_id: int,
        expected_version: Optional[int] = None,
    ):
        return self._diet_plan.apply_template(
            admission_id,
            shift_date,
            template_id,
            expected_version=expected_version,
        )

    def upsert_diet_plan(
        self,
        admission_id: int,
        shift_date: datetime,
        diet_text: str,
        schedule_json,
        template_id: Optional[int] = None,
        expected_version: Optional[int] = None,
    ):
        return self._diet_plan.upsert_plan(
            admission_id=admission_id,
            shift_date=shift_date,
            diet_text=diet_text,
            schedule_json=schedule_json,
            template_id=template_id,
            expected_version=expected_version,
        )

    def delete_diet_plan(self, admission_id: int, shift_date: datetime, expected_version: Optional[int] = None):
        return self._diet_plan.delete_plan(admission_id, shift_date, expected_version=expected_version)

    def get_oral_intake_events(self, admission_id: int, shift_date: datetime) -> List[OralIntakeEventDTO]:
        return self._oral_intake.get_events(admission_id, shift_date)

    def upsert_oral_intake_event(
        self,
        admission_id: int,
        event_time: datetime,
        amount_ml: Optional[float],
        expected_version: Optional[int] = None,
    ):
        return self._oral_intake.upsert_event(
            admission_id=admission_id,
            event_time=event_time,
            amount_ml=amount_ml,
            expected_version=expected_version,
        )

    def delete_oral_intake_event(
        self,
        admission_id: int,
        event_time: datetime,
        expected_version: Optional[int] = None,
    ):
        return self._oral_intake.delete_event(
            admission_id=admission_id,
            event_time=event_time,
            expected_version=expected_version,
        )

    def apply_oral_intake_changes(self, admission_id: int, changes: list[dict]):
        return self._oral_intake.apply_changes(admission_id, changes)

    def undo_oral_intake_changes(self, admission_id: int, shift_date: datetime, undo_batch: list[dict]):
        return self._oral_intake.undo_changes(admission_id, shift_date, undo_batch)

    def get_oral_intake_totals(self, admission_id: int, shift_date: datetime, current_time: Optional[datetime] = None) -> dict:
        return self._oral_intake.get_totals(admission_id, shift_date, current_time=current_time)

    # --- Order Service Methods ---
    def get_orders(self, admission_id: int, date=None, only_committed: bool = False) -> List[OrderDTO]:
        return self._orders.get_orders(admission_id, date, only_committed)

    def get_order_ids(self, admission_id: int, date=None, only_committed: bool = False) -> List[int]:
        return self._orders.get_order_ids(admission_id, date, only_committed)

    def add_order(self, dto: OrderDTO):
        return self._orders.add_order(dto)

    def add_orders_batch(self, orders: List[OrderDTO]):
        self._orders.add_orders_batch(orders)

    def update_order_status(self, order_id: int, status: str, expected_revision: Optional[int] = None):
        self._orders.update_order_status(order_id, status, expected_revision=expected_revision)

    def has_order_drafts(self, admission_id: int, shift_date: Optional[datetime] = None) -> bool:
        return self._orders.has_drafts(admission_id, shift_date=shift_date)

    def has_order_administrations(self, admission_id: int, shift_date: datetime, only_committed: bool = False) -> bool:
        return self._orders.has_administrations(admission_id, shift_date, only_committed=only_committed)

    def finalize_order_card(
        self,
        admission_id: int,
        *,
        shift_date: Optional[datetime] = None,
        ordered_order_ids=None,
        expected_revisions=None,
    ):
        self._orders.finalize_card(
            admission_id,
            shift_date=shift_date,
            ordered_order_ids=ordered_order_ids,
            expected_revisions=expected_revisions,
        )

    def save_order_draft_sort(self, admission_id: int, shift_date: datetime, ordered_order_ids, expected_revisions=None):
        self._orders.save_draft_order_sort(
            admission_id,
            shift_date,
            ordered_order_ids,
            expected_revisions=expected_revisions,
        )

    def clear_order_drafts(self, admission_id: int, shift_date: datetime, expected_revisions=None):
        self._orders.clear_drafts(admission_id, shift_date, expected_revisions=expected_revisions)

    def soft_delete_order_row(self, order_id: int, is_committed: bool, expected_revision: Optional[int] = None):
        self._orders.soft_delete_order_row(order_id, is_committed, expected_revision=expected_revision)

    def clear_order_times(self, admission_id: int, shift_date: datetime):
        self._orders.clear_all_times(admission_id, shift_date)

    def clear_order_list(self, admission_id: int, shift_date: datetime, expected_revisions=None):
        self._orders.clear_all_orders(admission_id, shift_date, expected_revisions=expected_revisions)

    def find_recent_orders_source(self, admission_id: int, shift_date: datetime, max_days_back: int = 3):
        return self._orders.find_recent_orders_source(admission_id, shift_date, max_days_back=max_days_back)

    def replace_orders_from_date(
        self,
        admission_id: int,
        target_shift_date: datetime,
        source_shift_date: datetime,
        source_orders: List[OrderDTO],
        expected_revisions=None,
    ):
        self._orders.replace_with_orders_from_date(
            admission_id=admission_id,
            target_shift_date=target_shift_date,
            source_shift_date=source_shift_date,
            source_orders=source_orders,
            expected_revisions=expected_revisions,
        )

    def get_latest_administrations(
        self,
        admission_id: int,
        shift_date: datetime,
        *,
        only_committed: bool = False,
        include_deleted: bool = False,
        include_cancelled: bool = False,
        include_deleted_orders: bool = True,
        updated_after: Optional[str] = None,
    ):
        return self._orders.get_latest_admin_rows(
            admission_id=admission_id,
            shift_date=shift_date,
            only_committed=only_committed,
            include_deleted=include_deleted,
            include_cancelled=include_cancelled,
            include_deleted_orders=include_deleted_orders,
            updated_after=updated_after,
        )

    def get_latest_administrations_for_order_ids(
        self,
        order_ids: Sequence[int],
        start_dt: datetime,
        end_dt: datetime,
        *,
        only_committed: bool = False,
        include_deleted: bool = False,
        include_cancelled: bool = False,
        include_deleted_orders: bool = True,
        updated_after: Optional[str] = None,
    ):
        return self._orders.get_latest_admin_rows_for_order_ids(
            order_ids=order_ids,
            start_dt=start_dt,
            end_dt=end_dt,
            only_committed=only_committed,
            include_deleted=include_deleted,
            include_cancelled=include_cancelled,
            include_deleted_orders=include_deleted_orders,
            updated_after=updated_after,
        )

    def apply_order_left_click(self, order: OrderDTO, admin, planned_time: datetime):
        return self._orders.apply_left_click(order, admin, planned_time)

    def apply_order_middle_click(self, order: OrderDTO, admin, planned_time: datetime):
        return self._orders.apply_middle_click(order, admin, planned_time)

    def apply_order_right_click(self, order: OrderDTO, admin, planned_time: datetime):
        return self._orders.apply_right_click(order, admin, planned_time)

    def set_nurse_order_mark(self, admin_id: int, mark: str, performer_id: Optional[int] = None):
        self._orders.set_nurse_status(admin_id, mark, performer_id=performer_id)

    def cancel_nurse_order_mark(self, admin_id: int):
        self._orders.cancel_nurse_action(admin_id)

    # Backward compatibility for widgets expecting OrderDomainService-like API
    def set_nurse_status(self, admin_id: int, mark: str, performer_id: Optional[int] = None):
        self._orders.set_nurse_status(admin_id, mark, performer_id=performer_id)

    def cancel_nurse_action(self, admin_id: int):
        self._orders.cancel_nurse_action(admin_id)

    def get_nurse_orders_data(self, admission_id: int, shift_date: datetime):
        return self._orders.get_nurse_orders_data(admission_id, shift_date)

    def get_nurse_statistics_rows(self, admission_ids: Sequence[int]):
        return self._orders.get_nurse_statistics_rows(admission_ids)

    # --- Shift Service Methods ---
    def normalize_time(self, value: str, fallback_time: Optional[str] = None) -> str:
        return self._shifts.normalize_time(value, fallback_time)

    def is_time_input_valid(self, value: str) -> bool:
        return self._shifts.is_time_input_valid(value)

    def resolve_datetime(self, time: str, shift_date: datetime) -> datetime:
        return self._shifts.resolve_datetime(time, shift_date)

    def apply_offset(self, time: str, shift_date: datetime, delta_minutes: int) -> str:
        return self._shifts.apply_offset(time, shift_date, delta_minutes)

    def now_time(self, current_dt: datetime, shift_date: datetime) -> str:
        return self._shifts.now_time(current_dt, shift_date)

    def current_shift_time(self, shift_date: datetime) -> str:
        return self._shifts.now_time(datetime.now(), shift_date)

    def next_full_hour(self, time: str, shift_date: datetime) -> str:
        return self._shifts.next_full_hour(time, shift_date)

    def display_hint(self, time: str, shift_date: datetime) -> Dict[str, object]:
        return self._shifts.display_hint(time, shift_date)

    def suggest_vital_time(
        self,
        shift_date: datetime,
        *,
        effective_start: Optional[datetime],
        effective_end: Optional[datetime],
        has_vitals: bool,
        current_dt: Optional[datetime] = None,
    ) -> str:
        now = current_dt or datetime.now()
        target_dt = effective_start or now
        if has_vitals:
            if effective_start and now < effective_start:
                target_dt = effective_start
            elif effective_end and now > effective_end:
                target_dt = effective_end
            else:
                target_dt = now
        return self._shifts.now_time(target_dt, shift_date)

    def get_day_period(self, date: datetime) -> Tuple[datetime, datetime]:
        return self._shifts.get_day_period(date)

    def get_effective_bounds(self, admission_id: int, date: datetime) -> Tuple[datetime, datetime]:
        return self._vitals.get_effective_bounds(admission_id, date)

    # --- General Data Management ---
    def has_changes(self, admission_id: int, last_sync_time: str) -> Tuple[bool, str]:
        """
        РџСЂРѕРІРµСЂСЏРµС‚ РЅР°Р»РёС‡РёРµ Р»СЋР±С‹С… РёР·РјРµРЅРµРЅРёР№ РІ РєР°СЂС‚Рµ РїР°С†РёРµРЅС‚Р° РїРѕСЃР»Рµ last_sync_time.
        Р’РѕР·РІСЂР°С‰Р°РµС‚ (bool, new_sync_time).
        """
        if not admission_id:
            return False, last_sync_time

        # РћРїСЂР°С€РёРІР°РµРј РјР°РєСЃРёРјР°Р»СЊРЅС‹Р№ updated_at РёР· РІСЃРµС… С‚Р°Р±Р»РёС† РґР°РЅРЅС‹С… РєР°СЂС‚С‹.
        # Р”РѕР±Р°РІР»СЏРµРј С‚Р°Р±Р»РёС†Сѓ administrations СЏРІРЅРѕ С‡РµСЂРµР· JOIN РґР»СЏ РЅР°РґРµР¶РЅРѕСЃС‚Рё.
        query = """
            SELECT MAX(ts) FROM (
                SELECT MAX(STRFTIME('%Y-%m-%d %H:%M:%f', updated_at)) as ts FROM vitals WHERE admission_id = ?
                UNION ALL
                SELECT MAX(STRFTIME('%Y-%m-%d %H:%M:%f', updated_at)) as ts FROM fluids WHERE admission_id = ?
                UNION ALL
                SELECT MAX(STRFTIME('%Y-%m-%d %H:%M:%f', updated_at)) as ts FROM orders WHERE admission_id = ?
                UNION ALL
                SELECT MAX(STRFTIME('%Y-%m-%d %H:%M:%f', a.updated_at)) as ts
                FROM administrations a
                JOIN orders o ON a.order_id = o.id
                WHERE o.admission_id = ?
                UNION ALL
                SELECT MAX(STRFTIME('%Y-%m-%d %H:%M:%f', updated_at)) as ts FROM patient_status_events WHERE admission_id = ?
                UNION ALL
                SELECT MAX(STRFTIME('%Y-%m-%d %H:%M:%f', updated_at)) as ts FROM diet_plan WHERE admission_id = ?
                UNION ALL
                SELECT MAX(STRFTIME('%Y-%m-%d %H:%M:%f', updated_at)) as ts FROM oral_intake_events WHERE admission_id = ?
            )
        """
        res = self.orders_dao.db.fetch_one_remcard(
            query,
            (
                admission_id,
                admission_id,
                admission_id,
                admission_id,
                admission_id,
                admission_id,
                admission_id,
            ),
        )
        
        last_sync_ts, last_sync_id = normalize_sync_cursor(last_sync_time)
        new_sync_ts, _ = normalize_sync_cursor(res[0] if res and res[0] else last_sync_ts)

        if is_cursor_newer(new_sync_ts, 0, last_sync_ts, last_sync_id):
            return True, new_sync_ts

        return False, last_sync_ts

    def delete_all_patient_data(self, admission_id: int):
        self._vitals.vitals_dao.delete_all_for_admission(admission_id)
        self._fluids.fluids_dao.delete_all_for_admission(admission_id)
        self._orders.dao.delete_all_for_admission(admission_id)
        self.orders_dao.db.execute_remcard("DELETE FROM diet_plan WHERE admission_id = ?", (admission_id,))
        self.orders_dao.db.execute_remcard("DELETE FROM oral_intake_events WHERE admission_id = ?", (admission_id,))

    def delete_last_card(self, admission_id: int):
        """
        РЈРґР°Р»СЏРµС‚ РїРѕСЃР»РµРґРЅСЋСЋ СЃРѕС…СЂР°РЅРµРЅРЅСѓСЋ РєР°СЂС‚Сѓ (СЃСѓС‚РєРё) РїР°С†РёРµРЅС‚Р°.
        Р’РѕР·РІСЂР°С‰Р°РµС‚ (ok: bool, last_date: Optional[datetime], message: str).
        """
        dates = self.get_all_card_dates(admission_id)
        if not dates:
            return False, None, "РЈ СЌС‚РѕРіРѕ РїР°С†РёРµРЅС‚Р° РЅРµС‚ СЃРѕС…СЂР°РЅРµРЅРЅС‹С… РєР°СЂС‚."

        last_date = max(dates)
        start_dt, end_dt = self.get_day_period(last_date)

        def operation(cursor):
            cursor.execute(
                "DELETE FROM vitals WHERE admission_id = ? AND datetime >= ? AND datetime < ?",
                (admission_id, start_dt.isoformat(), end_dt.isoformat()),
            )
            cursor.execute(
                "DELETE FROM fluids WHERE admission_id = ? AND datetime >= ? AND datetime < ?",
                (admission_id, start_dt.isoformat(), end_dt.isoformat()),
            )
            cursor.execute(
                "DELETE FROM diet_plan WHERE admission_id = ? AND shift_start >= ? AND shift_start < ?",
                (
                    admission_id,
                    start_dt.isoformat(timespec="minutes").replace("T", " "),
                    end_dt.isoformat(timespec="minutes").replace("T", " "),
                ),
            )
            cursor.execute(
                "DELETE FROM oral_intake_events WHERE admission_id = ? AND event_time >= ? AND event_time < ?",
                (
                    admission_id,
                    start_dt.isoformat(timespec="minutes").replace("T", " "),
                    end_dt.isoformat(timespec="minutes").replace("T", " "),
                ),
            )
            cursor.execute(
                """
                UPDATE administrations
                SET status = 'deleted',
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE order_id IN (SELECT id FROM orders WHERE admission_id = ?)
                  AND planned_time >= ? AND planned_time < ?
                """,
                (admission_id, start_dt.isoformat(), end_dt.isoformat()),
            )
            cursor.execute(
                """
                UPDATE orders
                SET updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE admission_id = ?
                """,
                (admission_id,),
            )

        self.run_write(f"delete_last_card:{admission_id}", operation)
        return True, last_date, "OK"

    def get_data_version(self) -> int:
        """Р’РѕР·РІСЂР°С‰Р°РµС‚ С‚РµРєСѓС‰СѓСЋ РІРµСЂСЃРёСЋ РґР°РЅРЅС‹С… РёР· С‚Р°Р±Р»РёС†С‹ meta."""
        if self.data_service:
            return self.data_service.get_data_version()
        query = "SELECT value FROM meta WHERE key = 'app_data_version'"
        res = self.orders_dao.db.fetch_one_remcard(query)
        return res[0] if res else 0

    def get_latest_change_id(self, admission_id: Optional[int] = None, include_global: bool = True) -> int:
        # РђРІС‚Рѕ-СЃРЅСЏС‚РёРµ СЃ РєРѕР№РєРё РЅСѓР¶РЅРѕ РІ РїРµСЂРІСѓСЋ РѕС‡РµСЂРµРґСЊ РґР»СЏ СЂРµР¶РёРјР° СЃРїРёСЃРєР° РєРѕРµРє (РіР»РѕР±Р°Р»СЊРЅС‹Р№ polling).
        # Р’ РїРѕРєР°СЂС‚РѕС‡РЅРѕРј polling (admission_id Р·Р°РґР°РЅ) РЅРµ Р·Р°РїСѓСЃРєР°РµРј С„РѕРЅРѕРІСѓСЋ Р·Р°РїРёСЃСЊ,
        # С‡С‚РѕР±С‹ РЅРµ СЃРѕР·РґР°РІР°С‚СЊ Р»РёС€РЅСЋСЋ РєРѕРЅРєСѓСЂРµРЅС†РёСЋ Р·Р° SQLite.
        if admission_id is None:
            self._patients.maybe_release_due_outcome_beds_async()
        if self.data_service:
            return self.data_service.get_latest_change_id(admission_id=admission_id, include_global=include_global)
        return self.orders_dao.db.get_latest_change_id(admission_id=admission_id, include_global=include_global)

    def get_changes_since(self, last_change_id: int, admission_id: Optional[int] = None, include_global: bool = True):
        if self.data_service:
            return self.data_service.fetch_changes_since(
                last_change_id=last_change_id,
                admission_id=admission_id,
                include_global=include_global,
            )
        return self.orders_dao.db.fetch_changes_since(
            last_change_id=last_change_id,
            admission_id=admission_id,
            include_global=include_global,
        )

    def get_changed_entities_since(
        self,
        last_change_id: int,
        admission_id: Optional[int] = None,
        include_global: bool = True,
    ) -> set[str]:
        if self.data_service:
            return self.data_service.get_changed_entities_since(
                last_change_id=last_change_id,
                admission_id=admission_id,
                include_global=include_global,
            )
        return self.orders_dao.db.get_changed_entities_since(
            last_change_id=last_change_id,
            admission_id=admission_id,
            include_global=include_global,
        )

    # --- Write Orchestration ---
    def run_write(self, description: str, operation: Callable):
        """
        Р•РґРёРЅР°СЏ СЃРёРЅС…СЂРѕРЅРЅР°СЏ С‚РѕС‡РєР° Р·Р°РїРёСЃРё.
        operation РґРѕР»Р¶РµРЅ РїСЂРёРЅРёРјР°С‚СЊ cursor Рё РІС‹РїРѕР»РЅСЏС‚СЊ write-Р»РѕРіРёРєСѓ РІРЅСѓС‚СЂРё РѕРґРЅРѕР№ С‚СЂР°РЅР·Р°РєС†РёРё.
        """
        if self.data_service:
            return self.data_service.run_write(description, operation)
        return self.orders_dao.db.run_write_operation(operation, source=description)

    def enqueue_write(
        self,
        description: str,
        operation: Callable[[], Any],
        on_success: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ):
        """
        РђСЃРёРЅС…СЂРѕРЅРЅР°СЏ Р·Р°РїРёСЃСЊ СЃ Р»РѕРєР°Р»СЊРЅРѕР№ РѕС‡РµСЂРµРґСЊСЋ.
        UI РїРѕР»СѓС‡Р°РµС‚ СЂРµР·СѓР»СЊС‚Р°С‚ С‡РµСЂРµР· on_success/on_error, РЅРµ СЂР°Р±РѕС‚Р°СЏ СЃ lock/retry РЅР°РїСЂСЏРјСѓСЋ.
        """
        if self.data_service:
            self.data_service.enqueue_write(
                description=description,
                operation=operation,
                on_success=on_success,
                on_error=on_error,
            )
            return

        try:
            result = operation()
            if on_success:
                on_success(result)
        except Exception as exc:
            if on_error:
                on_error(exc)
            else:
                raise

    # --- Ventilation Service Methods ---
    def _require_ventilation(self) -> VentilationService:
        if not self._ventilation:
            raise RuntimeError("Ventilation service is not configured.")
        return self._ventilation

    def get_mode_fields(self, mode: str) -> List[str]:
        return self._require_ventilation().get_mode_fields(mode)

    def create_case(self, admission_id: int, **kwargs) -> VentilationCaseDTO:
        return self._require_ventilation().create_case(admission_id, **kwargs)

    def add_event(self, case_id: int, **kwargs) -> VentilationEventDTO:
        return self._require_ventilation().add_event(case_id, **kwargs)

    def close_case(self, case_id: int, **kwargs) -> VentilationEventDTO:
        return self._require_ventilation().close_case(case_id, **kwargs)

    def replace_tube(self, case_id: int, **kwargs) -> VentilationEventDTO:
        return self._require_ventilation().replace_tube(case_id, **kwargs)

    def get_active_case(self, admission_id: int):
        return self._require_ventilation().get_active_case(admission_id)

    def get_latest_ventilation_case(self, admission_id: int):
        return self._require_ventilation().get_latest_case(admission_id)

    def get_total_vent_time(self, admission_id: int) -> float:
        return self._require_ventilation().get_total_vent_time(admission_id)

    def get_case_duration(self, case_id: int) -> float:
        return self._require_ventilation().get_case_duration(case_id)

    def get_ventilation_events(self, case_id: int):
        return self._require_ventilation().get_case_events(case_id)

    def get_ventilation_timeline(self, admission_id: int):
        return self._require_ventilation().get_admission_events(admission_id)

    def rollback_last_ventilation_action(self, case_id: int, **kwargs) -> VentilationEventDTO:
        return self._require_ventilation().rollback_last_action(case_id, **kwargs)

    def get_ventilation_summary(self, admission_id: int):
        return self._require_ventilation().get_active_case_summary(admission_id)


