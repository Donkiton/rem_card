import hashlib
import json
import time
from typing import List, Optional, Tuple, Dict, Any, Callable, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from PySide6.QtCore import QObject, Signal
from rem_card.app.logger import logger
from rem_card.app.local_metrics import record_metric
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
from ..data.dto.lab_orders_dto import LAB_MATERIAL_LABELS, LabOrderStatus
from .patient_service import PatientService
from .vital_service import VitalService
from .fluid_service import FluidService
from .diet_service import DietPlanService, DietTemplateService, OralIntakeService
from .order_service import OrderService
from .shift_service import ShiftService
from .ventilation_service import VentilationService

_ORDERS_SNAPSHOT_CALLER = ContextVar("remcard_orders_snapshot_caller", default="legacy")
_ORDERS_SNAPSHOT_CONTEXT_HASH = ContextVar("remcard_orders_snapshot_context_hash", default="unknown")
_ORDERS_SNAPSHOT_REQUEST_SOURCE = ContextVar("remcard_orders_snapshot_request_source", default="refresh")
_ORDERS_SNAPSHOT_STEP_OBSERVER = ContextVar("remcard_orders_snapshot_step_observer", default=None)
_DIRECT_ORDERS_BUILD_WARNED: set[tuple[str, int, str, str]] = set()
_LEGACY_ORDERS_ACCESS_COUNT = 0
_LAB_ORDER_CARD_PRIORITY = 998


class OrdersSnapshotCancelled(RuntimeError):
    """Controlled cancellation used to stop obsolete orders snapshot builds."""


@contextmanager
def orders_snapshot_caller(
    source: str,
    *,
    context_hash: Optional[str] = None,
    request_source: Optional[str] = None,
    step_observer: Optional[Callable[[str, str, Dict[str, Any]], None]] = None,
):
    token = _ORDERS_SNAPSHOT_CALLER.set(str(source or "legacy"))
    context_token = _ORDERS_SNAPSHOT_CONTEXT_HASH.set(str(context_hash or "unknown"))
    request_source_token = _ORDERS_SNAPSHOT_REQUEST_SOURCE.set(str(request_source or "refresh"))
    step_observer_token = _ORDERS_SNAPSHOT_STEP_OBSERVER.set(step_observer)
    try:
        yield
    finally:
        _ORDERS_SNAPSHOT_STEP_OBSERVER.reset(step_observer_token)
        _ORDERS_SNAPSHOT_REQUEST_SOURCE.reset(request_source_token)
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
        self._patients = PatientService(patient_dao, data_service=data_service)
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

        from rem_card.data.dao.procedures_dao import ProceduresDAO
        from rem_card.data.dao.lab_orders_dao import LabOrdersDAO
        from rem_card.services.lab_analysis_catalog_service import LabAnalysisCatalogService
        from rem_card.services.lab_orders_service import LabOrdersService
        from rem_card.services.procedures_print_service import ProceduresPrintService
        from rem_card.services.procedures_service import ProceduresService

        self.procedures_dao = ProceduresDAO(self.orders_dao.db)
        self._procedures = ProceduresService(self.procedures_dao, data_service=data_service)
        self._procedures_print = ProceduresPrintService(self.procedures_dao)
        self.lab_orders_dao = LabOrdersDAO(self.orders_dao.db)
        self._lab_analysis_catalog = LabAnalysisCatalogService()
        self._lab_orders = LabOrdersService(
            self.lab_orders_dao,
            data_service=data_service,
            catalog_service=self._lab_analysis_catalog,
        )
        self._connect_cache_invalidation()

    def _connect_cache_invalidation(self):
        if not self.data_service or not hasattr(self.data_service, "changes_detected"):
            return
        try:
            self.data_service.changes_detected.connect(self._handle_data_changes_for_cache)
        except Exception as exc:
            logger.warning("Failed to connect RemCardService cache invalidation: %s", exc)

    @staticmethod
    def _changed_entities_from_payload(payload: dict[str, Any]) -> set[str]:
        changed_entities = {
            str(entity)
            for entity in (payload.get("changed_entities") or [])
            if entity is not None
        }
        if changed_entities:
            return changed_entities
        return {
            str(change.get("entity_name") or "")
            for change in (payload.get("changes") or [])
            if change.get("entity_name")
        }

    def _handle_data_changes_for_cache(self, payload: dict):
        try:
            sync_actions = (payload or {}).get("sync_actions") or {}
            changed_entities = self._changed_entities_from_payload(payload or {})
            if "vital_settings" in changed_entities or sync_actions.get("full_refresh_required"):
                self._vitals.invalidate_cache()
                logger.info(
                    "RemCardService invalidated vital settings cache reason=%s",
                    "full_refresh" if sync_actions.get("full_refresh_required") else "vital_settings",
                )
        except Exception as exc:
            logger.warning("Failed to invalidate RemCardService caches after data changes: %s", exc)

    def has_card(self, admission_id: int, date: datetime) -> bool:
        """
        РџСЂРѕРІРµСЂСЏРµС‚ СЃСѓС‰РµСЃС‚РІРѕРІР°РЅРёРµ РєР°СЂС‚С‹ Р·Р° СѓРєР°Р·Р°РЅРЅСѓСЋ РґР°С‚Сѓ.
        РљР°СЂС‚Р° СЃС‡РёС‚Р°РµС‚СЃСЏ СЃСѓС‰РµСЃС‚РІСѓСЋС‰РµР№, РµСЃР»Рё РІ Р‘Р” РµСЃС‚СЊ С…РѕС‚СЊ РѕРґРЅР° Р·Р°РїРёСЃСЊ Р·Р° СЌС‚РѕС‚ РїРµСЂРёРѕРґ,
        РґР°Р¶Рµ РµСЃР»Рё РѕРЅР° РїРѕРјРµС‡РµРЅР° РєР°Рє 'deleted' (РЅРѕ СЏРІР»СЏРµС‚СЃСЏ СЃРѕС…СЂР°РЅРµРЅРЅРѕР№/СЃСѓС‰РµСЃС‚РІСѓСЋС‰РµР№ РІ Р‘Р”).
        """
        return bool(self.has_cards_bulk([int(admission_id)], date).get(int(admission_id), False))

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
                    OR EXISTS (
                        SELECT 1 FROM lab_orders lo
                        WHERE lo.admission_id = ids.admission_id
                          AND (
                            (lo.card_day_id = ?)
                            OR (
                                lo.card_day_id IS NULL
                                AND (
                                    (lo.scheduled_at IS NOT NULL AND DATETIME(lo.scheduled_at) >= DATETIME(?) AND DATETIME(lo.scheduled_at) < DATETIME(?))
                                    OR (
                                        lo.scheduled_at IS NULL
                                        AND lo.created_at IS NOT NULL
                                        AND DATETIME(lo.created_at) >= DATETIME(?)
                                        AND DATETIME(lo.created_at) < DATETIME(?)
                                    )
                                )
                            )
                          )
                    )
                    THEN 1
                    ELSE 0
                END AS has_card
            FROM ids
        """
        card_day_id = self._lab_orders.card_day_id_from_shift_start(start)
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
            card_day_id,
            start.isoformat(),
            end.isoformat(),
            start.isoformat(),
            end.isoformat(),
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

    def build_lab_orders_snapshot(
        self,
        admission_id: int,
        card_day_id: Optional[str] = None,
        *,
        shift_date: Optional[datetime] = None,
        include_change_cursor: bool = False,
    ) -> Dict[str, Any]:
        if isinstance(card_day_id, datetime) and shift_date is None:
            shift_date = card_day_id
            card_day_id = None

        effective_date = shift_date or datetime.now()
        if shift_date is None and card_day_id is not None:
            start_dt = None
            end_dt = None
            resolved_card_day_id = str(card_day_id)
        else:
            start_dt, end_dt = self.get_day_period(effective_date)
            resolved_card_day_id = str(card_day_id or self._lab_orders.card_day_id_from_shift_start(start_dt))
        snapshot = self._lab_orders.build_snapshot(
            int(admission_id),
            card_day_id=resolved_card_day_id,
            start_dt=start_dt,
            end_dt=end_dt,
        )
        snapshot.update(
            {
                "shift_date": effective_date,
                "start_dt": start_dt,
                "end_dt": end_dt,
                "card_day_id": resolved_card_day_id,
            }
        )
        if include_change_cursor:
            snapshot["change_id"] = self.get_latest_change_id(admission_id)
        return snapshot

    def list_lab_orders(self, admission_id: int, shift_date: Optional[datetime] = None):
        snapshot = self.build_lab_orders_snapshot(
            int(admission_id),
            shift_date=shift_date or datetime.now(),
            include_change_cursor=False,
        )
        return snapshot.get("rows") or []

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
                    active_intervals=snapshot.get("chart_active_intervals"),
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
        request_source = str(_ORDERS_SNAPSHOT_REQUEST_SOURCE.get() or "refresh").strip().lower()
        metric_source = "click" if request_source in {"user", "click"} else request_source
        if metric_source not in {"click", "post_finalize", "monitor", "cache"}:
            metric_source = "monitor"
        total_started = time.perf_counter()

        def _notify_orders_snapshot_step(event: str, step: str, **fields) -> None:
            observer = _ORDERS_SNAPSHOT_STEP_OBSERVER.get()
            if observer is None:
                return
            try:
                observer(event, step, fields)
            except OrdersSnapshotCancelled:
                raise
            except Exception:
                logger.debug("Orders snapshot step observer failed", exc_info=True)

        def _record_orders_snapshot_step_start(step: str) -> float:
            started = time.perf_counter()
            logger.info(
                "[RemCardService] orders_snapshot_step_start step=%s admission_id=%s caller=%s context_hash=%s",
                step,
                admission_id,
                caller,
                context_hash,
            )
            record_metric(
                "orders_snapshot_step_start",
                1,
                admission_id=admission_id,
                source=metric_source,
                step=step,
                caller=caller,
                request_source=request_source,
                context_hash=context_hash,
                only_committed=int(bool(only_committed)),
            )
            _notify_orders_snapshot_step("start", step, started=started)
            return started

        def _record_orders_snapshot_step_end(
            step: str,
            started: float,
            *,
            row_count=None,
            status: str = "ok",
        ) -> None:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            logger.info(
                "[RemCardService] orders_snapshot_step_end step=%s admission_id=%s caller=%s context_hash=%s status=%s elapsed_ms=%.2f row_count=%s",
                step,
                admission_id,
                caller,
                context_hash,
                status,
                elapsed_ms,
                row_count,
            )
            record_metric(
                "orders_snapshot_step_end",
                round(elapsed_ms, 3),
                admission_id=admission_id,
                source=metric_source,
                step=step,
                caller=caller,
                request_source=request_source,
                context_hash=context_hash,
                row_count=row_count,
                only_committed=int(bool(only_committed)),
                status=status,
            )
            _notify_orders_snapshot_step(
                "end",
                step,
                elapsed_ms=elapsed_ms,
                row_count=row_count,
                status=status,
            )
            logger.info(
                "[RemCardService] orders_snapshot_sql_step_ms=%.2f step=%s admission_id=%s caller=%s context_hash=%s row_count=%s status=%s",
                elapsed_ms,
                step,
                admission_id,
                caller,
                context_hash,
                row_count,
                status,
            )
            record_metric(
                "orders_snapshot_sql_step_ms",
                round(elapsed_ms, 3),
                admission_id=admission_id,
                source=metric_source,
                step=step,
                caller=caller,
                request_source=request_source,
                context_hash=context_hash,
                row_count=row_count,
                only_committed=int(bool(only_committed)),
                status=status,
            )

        def _orders_snapshot_cancel_check(step: str, started: float):
            def _check_cancelled() -> bool:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                _notify_orders_snapshot_step("poll", step, elapsed_ms=elapsed_ms)
                return False

            return _check_cancelled

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
        step_started = _record_orders_snapshot_step_start("get_orders")
        try:
            all_orders = self.get_orders(admission_id, shift_date, only_committed=only_committed)
        except Exception:
            _record_orders_snapshot_step_end("get_orders", step_started, status="error")
            raise
        _record_orders_snapshot_step_end("get_orders", step_started, row_count=len(all_orders or ()))

        step_started = _record_orders_snapshot_step_start("filter_visible_orders")
        try:
            visible_orders = (
                [order for order in all_orders if order]
                if only_committed
                else [order for order in all_orders if order and order.status != OrderStatus.DELETED]
            )
        except Exception:
            _record_orders_snapshot_step_end("filter_visible_orders", step_started, status="error")
            raise
        _record_orders_snapshot_step_end("filter_visible_orders", step_started, row_count=len(visible_orders))

        step_started = _record_orders_snapshot_step_start("get_latest_administrations")
        try:
            admin_rows = [
                dict(row)
                for row in self.get_latest_administrations(
                    admission_id=admission_id,
                    shift_date=shift_date,
                    only_committed=only_committed,
                    include_deleted=True,
                    include_cancelled=True,
                    include_deleted_orders=True,
                    cancel_check=_orders_snapshot_cancel_check("get_latest_administrations", step_started),
                )
            ]
        except OrdersSnapshotCancelled:
            _record_orders_snapshot_step_end("get_latest_administrations", step_started, status="cancelled")
            raise
        except Exception:
            _record_orders_snapshot_step_end("get_latest_administrations", step_started, status="error")
            raise
        _record_orders_snapshot_step_end("get_latest_administrations", step_started, row_count=len(admin_rows))

        step_started = _record_orders_snapshot_step_start("has_drafts")
        try:
            has_any_draft = self._orders.has_drafts(admission_id, shift_date=shift_date)
        except Exception:
            _record_orders_snapshot_step_end("has_drafts", step_started, status="error")
            raise
        _record_orders_snapshot_step_end("has_drafts", step_started)

        step_started = _record_orders_snapshot_step_start("finalize")
        try:
            has_any_administrations = any(
                str(row.get("status") or "") not in ("deleted", "cancelled")
                for row in admin_rows
            )

            snapshot: Dict[str, Any] = {
                "admission_id": admission_id,
                "shift_date": shift_date,
                "only_committed": bool(only_committed),
                "orders": visible_orders,
                "admin_rows": admin_rows,
                "has_any_draft": has_any_draft,
                "has_any_administrations": has_any_administrations,
                "has_any_orders": bool(visible_orders),
            }
        except Exception:
            _record_orders_snapshot_step_end("finalize", step_started, status="error")
            raise
        _record_orders_snapshot_step_end("finalize", step_started, row_count=len(visible_orders))
        if include_change_cursor:
            step_started = _record_orders_snapshot_step_start("get_latest_change_id")
            try:
                snapshot["change_id"] = self.get_latest_change_id(admission_id, include_global=False)
            except Exception:
                _record_orders_snapshot_step_end("get_latest_change_id", step_started, status="error")
                raise
            _record_orders_snapshot_step_end("get_latest_change_id", step_started)
        total_elapsed_ms = (time.perf_counter() - total_started) * 1000.0
        record_metric(
            "orders_snapshot_build_total_ms",
            round(total_elapsed_ms, 3),
            admission_id=admission_id,
            source=metric_source,
            caller=caller,
            request_source=request_source,
            context_hash=context_hash,
            orders_count=len(visible_orders),
            admin_rows_count=len(admin_rows),
            has_change_cursor=int(bool(include_change_cursor)),
        )
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
        active_intervals: Optional[Sequence[Tuple[datetime, datetime]]] = None,
    ) -> Dict[str, Any]:
        from ..data.dto.remcard_dto import AdministrationDTO
        from .balance_calculator import BalanceCalculator

        if hasattr(self._fluids, "get_balance_bounds_for_state"):
            effective_start, effective_end = self._fluids.get_balance_bounds_for_state(
                admission_id,
                shift_date,
                patient=patient,
                current_status=current_status,
                shift_bounds=(start_dt, end_dt),
            )
        elif hasattr(self._fluids, "get_balance_bounds"):
            effective_start, effective_end = self._fluids.get_balance_bounds(admission_id, shift_date)
        else:
            effective_start, effective_end = self.get_effective_bounds(admission_id, shift_date)
        if hasattr(self._fluids, "get_fluids_in_bounds"):
            fluids = self._fluids.get_fluids_in_bounds(admission_id, effective_start, effective_end)
        else:
            fluids = self.get_fluids(admission_id, shift_date)
        orders = self.get_orders(admission_id, shift_date, only_committed=only_committed)

        order_ids = [
            int(order.id)
            for order in orders
            if getattr(order, "id", None) is not None
        ]
        admin_rows = (
            self.get_latest_administrations_for_order_ids(
                order_ids=order_ids,
                start_dt=start_dt,
                end_dt=end_dt,
                only_committed=only_committed,
                include_deleted=False,
                include_cancelled=False,
                include_deleted_orders=True,
            )
            if order_ids
            else []
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

        if active_intervals is not None:
            active_intervals = self._clip_active_intervals(active_intervals, start_dt, end_dt)
        else:
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

    @staticmethod
    def _clip_active_intervals(
        intervals: Sequence[Tuple[datetime, datetime]],
        start_dt: datetime,
        end_dt: datetime,
    ) -> List[Tuple[datetime, datetime]]:
        clipped: List[Tuple[datetime, datetime]] = []
        for interval_start, interval_end in intervals or []:
            overlap_start = max(interval_start, start_dt)
            overlap_end = min(interval_end, end_dt)
            if overlap_start <= overlap_end:
                clipped.append((overlap_start, overlap_end))
        return clipped

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

    @staticmethod
    def _row_value(row, key: str, index: int = 0):
        if row is None:
            return None
        try:
            return row[key]
        except Exception:
            try:
                return row[index]
            except Exception:
                return None

    def get_emergency_notice(self, admission_id: int, *, force_central: bool = False) -> Dict[str, Any]:
        query = """
            SELECT emergency_notice_number, emergency_notice_entered_at
            FROM admissions
            WHERE id = ?
        """
        params = (int(admission_id),)
        try:
            db = self.orders_dao.db
            central_fetch = getattr(db, "_fetch_one_central_with_retry", None)
            if force_central and callable(central_fetch):
                row = central_fetch(query, params)
            else:
                row = db.fetch_one_remcard(query, params)
        except Exception as exc:
            if "emergency_notice_" not in str(exc).lower():
                raise
            return {"number": "", "entered_at": None}
        if not row:
            return {"number": "", "entered_at": None}
        return {
            "number": str(self._row_value(row, "emergency_notice_number", 0) or "").strip(),
            "entered_at": self._row_value(row, "emergency_notice_entered_at", 1),
        }

    def save_emergency_notice(
        self,
        admission_id: int,
        number: str,
        shift_date: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        admission_id = int(admission_id)
        normalized = str(number or "").strip()
        shift_start, _shift_end = self.get_day_period(shift_date or datetime.now())
        default_entered_at = shift_start.replace(microsecond=0).isoformat()
        now_text = datetime.now().replace(microsecond=0).isoformat()

        def operation(cursor):
            cursor.execute(
                """
                SELECT emergency_notice_number, emergency_notice_entered_at
                FROM admissions
                WHERE id = ?
                """,
                (admission_id,),
            )
            row = cursor.fetchone()
            if not row:
                raise ValueError("Госпитализация пациента не найдена.")

            old_number = str(self._row_value(row, "emergency_notice_number", 0) or "").strip()
            old_entered_at = self._row_value(row, "emergency_notice_entered_at", 1)

            if normalized:
                entered_at = str(old_entered_at or "").strip() if old_number else default_entered_at
                if not entered_at:
                    entered_at = default_entered_at
            else:
                entered_at = None

            stored_number = normalized or None
            if old_number == normalized and (old_entered_at or None) == (entered_at or None):
                return {"number": normalized, "entered_at": entered_at}

            cursor.execute(
                """
                UPDATE admissions
                SET emergency_notice_number = ?,
                    emergency_notice_entered_at = ?,
                    updated_at = ?,
                    revision = COALESCE(revision, 0) + 1
                WHERE id = ?
                """,
                (stored_number, entered_at, now_text, admission_id),
            )
            return {"number": normalized, "entered_at": entered_at}

        result = self.run_write(f"emergency_notice_save:{admission_id}", operation)
        return dict(result or {"number": normalized, "entered_at": None})

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
        lab_dates = self._get_lab_orders_raw_dates(admission_id)
        
        # РћР±СЉРµРґРёРЅСЏРµРј РІСЃРµ "СЃС‹СЂС‹Рµ" РґР°С‚С‹ РІ РѕРґРёРЅ СЃРїРёСЃРѕРє
        raw_dates = list(set(v_dates + o_dates + f_dates + diet_dates + lab_dates))
        
        return self._shifts.get_all_card_dates(raw_dates)

    def get_latest_card_date(self, admission_id: int) -> Optional[datetime]:
        """Р’РѕР·РІСЂР°С‰Р°РµС‚ РґР°С‚Сѓ РїРѕСЃР»РµРґРЅРµР№ Р·Р°РїРёСЃРё РґР»СЏ РїР°С†РёРµРЅС‚Р° (РїРѕ РІРёС‚Р°Р»Р°Рј, Р¶РёРґРєРѕСЃС‚СЏРј РёР»Рё РЅР°Р·РЅР°С‡РµРЅРёСЏРј)."""
        v_dates = self.vitals_dao.get_all_vital_dates(admission_id)
        o_dates = self.orders_dao.get_all_dates(admission_id)
        f_dates = self.fluids_dao.get_all_dates(admission_id)
        diet_dates = self._get_diet_raw_dates(admission_id)
        lab_dates = self._get_lab_orders_raw_dates(admission_id)
        
        all_dates = v_dates + o_dates + f_dates + diet_dates + lab_dates
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

    def _get_lab_orders_raw_dates(self, admission_id: int) -> List[datetime]:
        rows = self.orders_dao.db.fetch_all_remcard(
            """
            SELECT COALESCE(scheduled_at, created_at, completed_at) AS dt
            FROM lab_orders
            WHERE admission_id = ?
            """,
            (int(admission_id),),
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

    def reorder_diet_templates(self, ordered_template_ids: list[int]):
        return self._diet_templates.reorder_templates(ordered_template_ids)

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

    def update_order(self, order_id: int, dto: OrderDTO, expected_revision: Optional[int] = None):
        return self._orders.update_order(order_id, dto, expected_revision=expected_revision)

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

    def repair_order_draft_integrity(self, admission_id: int, shift_date: Optional[datetime] = None):
        return self._orders.repair_draft_integrity(admission_id, shift_date)

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
        cancel_check=None,
    ):
        return self._orders.get_latest_admin_rows(
            admission_id=admission_id,
            shift_date=shift_date,
            only_committed=only_committed,
            include_deleted=include_deleted,
            include_cancelled=include_cancelled,
            include_deleted_orders=include_deleted_orders,
            updated_after=updated_after,
            cancel_check=cancel_check,
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
        cancel_check=None,
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
            cancel_check=cancel_check,
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

    def set_doctor_order_mark(self, admin_id: int, mark: str, performer_id: Optional[int] = None):
        self._orders.set_doctor_status(admin_id, mark, performer_id=performer_id)

    def cancel_doctor_order_mark(self, admin_id: int):
        self._orders.cancel_doctor_action(admin_id)

    # Backward compatibility for widgets expecting OrderDomainService-like API
    def set_nurse_status(self, admin_id: int, mark: str, performer_id: Optional[int] = None):
        self._orders.set_nurse_status(admin_id, mark, performer_id=performer_id)

    def cancel_nurse_action(self, admin_id: int):
        self._orders.cancel_nurse_action(admin_id)

    def set_doctor_status(self, admin_id: int, mark: str, performer_id: Optional[int] = None):
        self._orders.set_doctor_status(admin_id, mark, performer_id=performer_id)

    def cancel_doctor_action(self, admin_id: int):
        self._orders.cancel_doctor_action(admin_id)

    @staticmethod
    def _card_datetime(value: Any) -> Optional[datetime]:
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            return value
        text = str(value).strip()
        if not text:
            return None
        for candidate in (text, text.replace(" ", "T")):
            try:
                return datetime.fromisoformat(candidate)
            except ValueError:
                if "." in candidate:
                    head, frac = candidate.split(".", 1)
                    try:
                        return datetime.fromisoformat(f"{head}.{frac[:6]}")
                    except ValueError:
                        pass
        return None

    @classmethod
    def _card_datetime_text(cls, value: Any) -> str:
        parsed = cls._card_datetime(value)
        if parsed is not None:
            return parsed.isoformat(timespec="seconds")
        return str(value or "").strip()

    @staticmethod
    def _lab_order_card_id(order_id: Any) -> int:
        return -abs(int(order_id))

    @classmethod
    def _signal_state_for_card_time(cls, planned_time: Any) -> str:
        planned_dt = cls._card_datetime(planned_time)
        if planned_dt is None:
            return ""
        diff_minutes = (datetime.now() - planned_dt).total_seconds() / 60.0
        if -60 <= diff_minutes < 0:
            return "upcoming"
        if 0 <= diff_minutes < 60:
            return "current"
        if 60 <= diff_minutes < 180:
            return "overdue"
        return ""

    @staticmethod
    def _bed_sort_key(value: Any) -> tuple[int, int, str]:
        raw = str(value if value is not None else "").strip()
        if not raw:
            return (1, 999999, "")
        try:
            return (0, int(raw), raw)
        except ValueError:
            return (0, 999999, raw.lower())

    @classmethod
    def _upcoming_card_sort_key(cls, row: dict[str, Any]) -> tuple:
        planned = cls._card_datetime(row.get("planned_time")) or datetime.max
        return (
            cls._bed_sort_key(row.get("bed_number")),
            planned.isoformat(),
            int(row.get("priority") or 999),
            str(row.get("source_type") or "order"),
            abs(int(row.get("id") or 0)),
        )

    def _lab_order_card_payload(self, row: Any, *, include_patient: bool = False) -> Optional[dict[str, Any]]:
        data = dict(row or {})
        lab_order_id = data.get("lab_order_id", data.get("id"))
        if lab_order_id is None:
            return None
        scheduled_text = self._card_datetime_text(data.get("scheduled_at"))
        if not scheduled_text:
            return None

        material = str(data.get("material") or "").strip()
        try:
            material_labels = self._lab_analysis_catalog.material_labels()
        except Exception:
            material_labels = dict(LAB_MATERIAL_LABELS)
        material_label = material_labels.get(material, material or "Материал не указан")
        analysis_name = str(data.get("analysis_name") or "Анализ").strip() or "Анализ"
        revision = int(data.get("revision") or 0)
        card_id = self._lab_order_card_id(lab_order_id)
        payload = {
            "id": card_id,
            "admin_id": card_id,
            "source_type": "lab_order",
            "lab_order_id": int(lab_order_id),
            "patient_id": data.get("patient_id"),
            "admission_id": data.get("admission_id"),
            "planned_time": scheduled_text,
            "actual_time": self._card_datetime_text(data.get("completed_at")),
            "status": "planned",
            "comment": "",
            "cell_role": "single",
            "expected_revision": revision,
            "order_id": None,
            "order_title": analysis_name,
            "latin": analysis_name,
            "drug_key": "lab_order",
            "dose_value": None,
            "dose_unit": "",
            "order_comment": "",
            "order_type": "lab_order",
            "duration_min": 0,
            "order_revision": revision,
            "priority": _LAB_ORDER_CARD_PRIORITY,
            "group_name": "lab_orders",
            "analysis_code": data.get("analysis_code"),
            "analysis_name": analysis_name,
            "material": material,
            "material_label": material_label,
            "lab_comment": str(data.get("comment") or "").strip(),
            "created_at": self._card_datetime_text(data.get("created_at")),
            "updated_at": self._card_datetime_text(data.get("updated_at")),
            "allow_not_done": False,
            "signal_state": self._signal_state_for_card_time(scheduled_text),
        }
        if include_patient:
            payload["patient_name"] = data.get("patient_name")
            payload["patient_full_name"] = data.get("patient_name")
            payload["bed_number"] = data.get("bed_number")
        return payload

    def _lab_order_cards_for_admission(self, admission_id: int, shift_date: datetime) -> list[dict[str, Any]]:
        start_dt, end_dt = self.get_day_period(shift_date)
        rows = self.orders_dao.db.fetch_all_remcard(
            """
            SELECT
                lo.id AS lab_order_id,
                lo.patient_id,
                lo.admission_id,
                lo.card_day_id,
                lo.analysis_code,
                lo.analysis_name,
                lo.material,
                lo.status,
                lo.created_at,
                lo.scheduled_at,
                lo.completed_at,
                lo.comment,
                lo.created_by_role,
                lo.created_by_user,
                lo.completed_by_role,
                lo.completed_by_user,
                lo.revision,
                lo.updated_at
            FROM lab_orders lo
            JOIN admissions adm ON adm.id = lo.admission_id
            WHERE lo.admission_id = ?
              AND lo.scheduled_at IS NOT NULL
              AND DATETIME(lo.scheduled_at) >= DATETIME(?)
              AND DATETIME(lo.scheduled_at) < DATETIME(?)
              AND LOWER(COALESCE(NULLIF(TRIM(lo.status), ''), ?)) = ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM patient_status_events pse_out
                  WHERE pse_out.admission_id = adm.id
                    AND pse_out.end_time IS NULL
                    AND pse_out.status IN ('TRANSFERRED', 'DEAD')
              )
              AND TRIM(COALESCE(adm.outcome, '')) NOT IN (
                  'переведен', 'переведён', 'умер',
                  'Переведен', 'Переведён', 'Умер',
                  'transferred', 'TRANSFERRED', 'dead', 'DEAD'
              )
              AND NULLIF(TRIM(COALESCE(adm.transfer_datetime, '')), '') IS NULL
              AND NULLIF(TRIM(COALESCE(adm.death_datetime, '')), '') IS NULL
            ORDER BY DATETIME(lo.scheduled_at) ASC, lo.id ASC
            """,
            (
                int(admission_id),
                start_dt.isoformat(timespec="seconds"),
                end_dt.isoformat(timespec="seconds"),
                LabOrderStatus.ASSIGNED.value,
                LabOrderStatus.ASSIGNED.value,
            ),
        )
        return [
            payload
            for payload in (self._lab_order_card_payload(row) for row in rows)
            if payload is not None
        ]

    def _lab_order_cards_across_active_admissions(self, shift_date: datetime) -> list[dict[str, Any]]:
        start_dt, end_dt = self.get_day_period(shift_date)
        rows = self.orders_dao.db.fetch_all_remcard(
            """
            SELECT
                lo.id AS lab_order_id,
                lo.patient_id,
                lo.admission_id,
                lo.card_day_id,
                lo.analysis_code,
                lo.analysis_name,
                lo.material,
                lo.status,
                lo.created_at,
                lo.scheduled_at,
                lo.completed_at,
                lo.comment,
                lo.created_by_role,
                lo.created_by_user,
                lo.completed_by_role,
                lo.completed_by_user,
                lo.revision,
                lo.updated_at,
                b.bed_number AS bed_number,
                COALESCE(
                    NULLIF(TRIM(
                        COALESCE(p.last_name, '') || ' ' ||
                        COALESCE(p.first_name, '') || ' ' ||
                        COALESCE(p.middle_name, '')
                    ), ''),
                    NULLIF(TRIM(COALESCE(p.full_name, '')), ''),
                    'Неизвестно'
                ) AS patient_name
            FROM lab_orders lo
            JOIN admissions adm ON adm.id = lo.admission_id
            JOIN patients p ON p.id = adm.patient_id
            JOIN beds b ON b.current_admission_id = adm.id AND b.status = 'OCCUPIED'
            WHERE lo.scheduled_at IS NOT NULL
              AND DATETIME(lo.scheduled_at) >= DATETIME(?)
              AND DATETIME(lo.scheduled_at) < DATETIME(?)
              AND LOWER(COALESCE(NULLIF(TRIM(lo.status), ''), ?)) = ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM patient_status_events pse_out
                  WHERE pse_out.admission_id = adm.id
                    AND pse_out.end_time IS NULL
                    AND pse_out.status IN ('TRANSFERRED', 'DEAD')
              )
              AND TRIM(COALESCE(adm.outcome, '')) NOT IN (
                  'переведен', 'переведён', 'умер',
                  'Переведен', 'Переведён', 'Умер',
                  'transferred', 'TRANSFERRED', 'dead', 'DEAD'
              )
              AND NULLIF(TRIM(COALESCE(adm.transfer_datetime, '')), '') IS NULL
              AND NULLIF(TRIM(COALESCE(adm.death_datetime, '')), '') IS NULL
            ORDER BY CAST(b.bed_number AS INTEGER) ASC, b.bed_number ASC, DATETIME(lo.scheduled_at) ASC, lo.id ASC
            """,
            (
                start_dt.isoformat(timespec="seconds"),
                end_dt.isoformat(timespec="seconds"),
                LabOrderStatus.ASSIGNED.value,
                LabOrderStatus.ASSIGNED.value,
            ),
        )
        return [
            payload
            for payload in (self._lab_order_card_payload(row, include_patient=True) for row in rows)
            if payload is not None
        ]

    def get_nurse_orders_data(self, admission_id: int, shift_date: datetime):
        rows = [
            dict(row)
            for row in self._orders.get_nurse_orders_data(admission_id, shift_date)
        ]
        rows.extend(self._lab_order_cards_for_admission(int(admission_id), shift_date))
        return rows

    @staticmethod
    def _upcoming_orders_content_hash(rows: Sequence[dict]) -> str:
        stable_rows = []
        volatile_keys = {"signal_state"}
        for row in rows or []:
            stable_rows.append(
                {
                    str(key): value
                    for key, value in sorted(dict(row).items())
                    if key not in volatile_keys
                }
            )
        payload = json.dumps(stable_rows, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def build_w1a_upcoming_orders_snapshot(self, shift_date: Optional[datetime] = None) -> Dict[str, Any]:
        effective_shift_date = shift_date or datetime.now()
        rows = [
            dict(row)
            for row in self._orders.get_upcoming_orders_across_active_admissions(effective_shift_date)
        ]
        rows.extend(self._lab_order_cards_across_active_admissions(effective_shift_date))
        rows.sort(key=self._upcoming_card_sort_key)
        if self.data_service and hasattr(self.data_service, "get_latest_change_id"):
            change_id = self.data_service.get_latest_change_id(admission_id=None, include_global=True)
        elif hasattr(self.orders_dao.db, "get_latest_change_id"):
            change_id = self.orders_dao.db.get_latest_change_id(admission_id=None, include_global=True)
        else:
            change_id = 0
        return {
            "scope": "w1a_upcoming_orders",
            "shift_date": effective_shift_date,
            "rows": rows,
            "change_id": change_id,
            "content_hash": self._upcoming_orders_content_hash(rows),
        }

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
                UNION ALL
                SELECT MAX(STRFTIME('%Y-%m-%d %H:%M:%f', updated_at)) as ts FROM admissions WHERE id = ?
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
        self.orders_dao.db.execute_remcard("DELETE FROM lab_orders WHERE admission_id = ?", (admission_id,))

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
            self.lab_orders_dao.delete_for_card_day(
                cursor,
                admission_id,
                card_day_id=self._lab_orders.card_day_id_from_shift_start(start_dt),
                start_dt=start_dt,
                end_dt=end_dt,
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

    def get_observed_change_state(self):
        if self.data_service and hasattr(self.data_service, "get_observed_change_state"):
            return self.data_service.get_observed_change_state()
        return None

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

    # --- Lab Orders ---
    def create_lab_order(self, **kwargs):
        return self._lab_orders.create_lab_order(**kwargs)

    def create_lab_orders(
        self,
        admission_id: int,
        *,
        orders: Sequence[dict[str, Any]],
        shift_date: Optional[datetime] = None,
        card_day_id: Optional[str] = None,
        created_by_role: str = "doctor",
        created_by_user: Optional[str] = None,
    ):
        effective_date = shift_date or datetime.now()
        if card_day_id is None:
            start_dt, _ = self.get_day_period(effective_date)
            card_day_id = self._lab_orders.card_day_id_from_shift_start(start_dt)
        return self._lab_orders.create_lab_orders(
            admission_id=int(admission_id),
            card_day_id=card_day_id,
            orders=orders,
            created_by_role=created_by_role,
            created_by_user=created_by_user,
        )

    def list_lab_analysis_templates(self):
        return self._lab_analysis_catalog.list_templates()

    def list_lab_materials(self):
        return self._lab_analysis_catalog.list_materials()

    def create_lab_material(self, **kwargs):
        return self._lab_analysis_catalog.create_material(**kwargs)

    def save_lab_materials(self, materials: list[dict[str, Any]]):
        return self._lab_analysis_catalog.save_materials(materials)

    def create_lab_analysis_template(self, **kwargs):
        return self._lab_analysis_catalog.create_template(**kwargs)

    def update_lab_analysis_template(self, template_id: int, **kwargs):
        return self._lab_analysis_catalog.update_template(int(template_id), **kwargs)

    def delete_lab_analysis_template(self, template_id: int, **kwargs):
        return self._lab_analysis_catalog.delete_template(int(template_id), **kwargs)

    def reorder_lab_analysis_templates(self, ordered_template_ids: list[int]):
        return self._lab_analysis_catalog.reorder_templates(ordered_template_ids)

    def update_lab_order_details(self, order_id: int, **kwargs):
        return self._lab_orders.update_lab_order_details(int(order_id), **kwargs)

    def delete_lab_orders(self, admission_id: int, *, order_ids: Sequence[int]):
        return self._lab_orders.delete_lab_orders(int(admission_id), order_ids=order_ids)

    def mark_lab_order_completed(self, order_id: int, **kwargs):
        return self._lab_orders.mark_completed(int(order_id), **kwargs)

    # --- Procedures ---
    def list_procedures(self, admission_id: int):
        return self._procedures.list_procedures(admission_id)

    def get_procedure_bundle(self, procedure_id: int):
        return self._procedures.get_procedure_bundle(procedure_id)

    def create_empty_cvc_procedure(self, admission_id: int, *, doctor_name: str = ""):
        return self._procedures.create_empty_cvc(admission_id, doctor_name=doctor_name)

    def create_empty_lumbar_puncture_procedure(self, admission_id: int, *, doctor_name: str = ""):
        return self._procedures.create_empty_lumbar_puncture(admission_id, doctor_name=doctor_name)

    def create_empty_transfusion_procedure(self, admission_id: int, *, doctor_name: str = ""):
        return self._procedures.create_empty_transfusion(admission_id, doctor_name=doctor_name)

    def save_cvc_procedure(self, procedure, cvc, consent):
        return self._procedures.save_cvc_procedure(procedure, cvc, consent)

    def save_lumbar_puncture_procedure(self, procedure, lumbar_puncture, consent):
        return self._procedures.save_lumbar_puncture_procedure(procedure, lumbar_puncture, consent)

    def save_transfusion_procedure(self, procedure, transfusion, consent):
        return self._procedures.save_transfusion_procedure(procedure, transfusion, consent)

    def get_transfusion_observation_values(self, admission_id: int, started_at, finished_at):
        values = self._vitals.get_transfusion_observation_values(admission_id, started_at, finished_at)
        try:
            diuresis_values = self._fluids.get_transfusion_diuresis_values(admission_id, started_at, finished_at)
        except Exception:
            diuresis_values = {}
        for slot, diuresis in (diuresis_values or {}).items():
            values.setdefault(slot, {})
            if diuresis:
                values[slot]["diuresis"] = diuresis
        return values

    def get_transfusion_registration_sheet(self, admission_id: int, *, start_dt=None, end_dt=None):
        return self._procedures.get_transfusion_registration_sheet(admission_id, start_dt=start_dt, end_dt=end_dt)

    def get_unprinted_completed_transfusion_protocols(self, admission_id: int, *, start_dt=None, end_dt=None):
        self._procedures.refresh_transfusion_statuses(admission_id)
        return self._procedures_print.unprinted_completed_transfusion_protocols(
            admission_id,
            start_dt=start_dt,
            end_dt=end_dt,
        )

    def mark_transfusion_protocols_printed(self, procedure_ids):
        return self._procedures.mark_protocols_printed([int(value) for value in procedure_ids if value])

    def cancel_procedure(self, procedure_id: int, *, updated_by: str = "doctor"):
        return self._procedures.cancel_procedure(procedure_id, updated_by=updated_by)

    def render_procedure_document(self, procedure_id: int, document_kind: str) -> str:
        return self._procedures_print.render_document(procedure_id, document_kind)

    def build_procedure_pdf_path(self, procedure_id: int, document_kind: str):
        return self._procedures_print.build_pdf_path(procedure_id, document_kind)

    def build_procedure_pdf(self, procedure_id: int, document_kind: str, pdf_path):
        result = self._procedures_print.build_pdf(procedure_id, document_kind, pdf_path)
        if str(document_kind or "").strip() == "transfusion_protocol":
            self._procedures.mark_protocols_printed([int(procedure_id)])
        return result


