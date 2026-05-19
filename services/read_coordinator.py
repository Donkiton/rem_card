import heapq
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha1
from itertools import count
from types import MappingProxyType
from typing import Any, Dict, List, Optional, Tuple

from rem_card.app.foreground_activity import foreground_read
from rem_card.app.logger import logger
from rem_card.app.local_metrics import record_metric
from rem_card.data.dao.sync_cursor import EPOCH_SYNC_TS, is_cursor_newer, make_sync_cursor
from rem_card.services import persistent_snapshot_cache
from rem_card.services.remcard_facade import orders_snapshot_caller


READ_CACHE_MAX_PATIENTS = min(10, max(3, int(os.environ.get("REMCARD_READ_CACHE_PATIENTS", "10"))))
READ_CACHE_MAX_TABS_PER_PATIENT = min(4, max(3, int(os.environ.get("REMCARD_READ_CACHE_TABS_PER_PATIENT", "4"))))
READ_MAX_CONCURRENT_LOADS = min(3, max(1, int(os.environ.get("REMCARD_READ_MAX_CONCURRENT_LOADS", "2"))))
READ_LOAD_TIMEOUT_SEC = max(10.0, float(os.environ.get("REMCARD_READ_LOAD_TIMEOUT_SEC", "15")))
READ_ORDERS_DELTA_STALE_THRESHOLD_SEC = max(
    5.0,
    float(os.environ.get("REMCARD_ORDERS_DELTA_STALE_THRESHOLD_SEC", "10")),
)
READ_ORDERS_TELEMETRY_LOG_EVERY = max(10, int(os.environ.get("REMCARD_ORDERS_TELEMETRY_LOG_EVERY", "20")))
READ_ORDERS_TELEMETRY_LOG_INTERVAL_SEC = max(
    60.0,
    float(os.environ.get("REMCARD_ORDERS_TELEMETRY_LOG_INTERVAL_SEC", "300")),
)
READ_MONITOR_STATE_MAX_AGE_SEC = max(
    2.0,
    float(os.environ.get("REMCARD_READ_MONITOR_STATE_MAX_AGE_SEC", "5")),
)

_VALID_ROLES = {"doctor", "nurse"}
_VALID_MODES = {"live", "archive"}
_PRIORITY_WEIGHTS = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


@dataclass(frozen=True)
class SnapshotContext:
    source_db: str
    admission_id: int
    shift_date: datetime
    role: str
    mode: str
    variant: str

    def __post_init__(self):
        normalized_shift_date = self._normalize_shift_date(self.shift_date)
        normalized_role = self._normalize_role(self.role)
        normalized_mode = self._normalize_mode(self.mode)
        normalized_variant = str(self.variant or "").strip().lower()
        normalized_source_db = self._normalize_source_db(self.source_db, normalized_mode)
        object.__setattr__(self, "shift_date", normalized_shift_date)
        object.__setattr__(self, "role", normalized_role)
        object.__setattr__(self, "mode", normalized_mode)
        object.__setattr__(self, "variant", normalized_variant)
        object.__setattr__(self, "source_db", normalized_source_db)
        object.__setattr__(self, "admission_id", int(self.admission_id))

    @staticmethod
    def _normalize_shift_date(value: datetime) -> datetime:
        if not isinstance(value, datetime):
            raise TypeError("shift_date must be a datetime")
        return value.replace(microsecond=0)

    @staticmethod
    def _normalize_role(value: str) -> str:
        role = str(value or "").strip().lower()
        if role not in _VALID_ROLES:
            raise ValueError(f"Unsupported role: {value!r}")
        return role

    @staticmethod
    def _normalize_mode(value: str) -> str:
        mode = str(value or "").strip().lower()
        if mode not in _VALID_MODES:
            raise ValueError(f"Unsupported mode: {value!r}")
        return mode

    @staticmethod
    def _normalize_source_db(value: str, mode: str) -> str:
        if mode == "live":
            return "live"
        raw = str(value or "").strip()
        return os.path.abspath(raw) if raw else "archive"

    def shift_key(self) -> str:
        return self.shift_date.isoformat(timespec="seconds")

    def hash(self) -> str:
        payload = "|".join(
            [
                self.source_db,
                str(self.admission_id),
                self.shift_key(),
                self.role,
                self.mode,
                self.variant,
            ]
        )
        return sha1(payload.encode("utf-8")).hexdigest()[:16]

    def cache_key(self) -> Tuple[str, int, str, str, str, str, str]:
        return (
            self.source_db,
            self.admission_id,
            self.shift_key(),
            self.role,
            self.mode,
            self.variant,
            self.hash(),
        )


@dataclass(frozen=True)
class OrdersContext(SnapshotContext):
    pass


@dataclass(frozen=True)
class PatientSnapshotContext(SnapshotContext):
    @staticmethod
    def _normalize_shift_date(value: datetime) -> datetime:
        normalized = SnapshotContext._normalize_shift_date(value)
        shift_start = normalized.replace(hour=8, minute=0, second=0, microsecond=0)
        if normalized.hour < 8:
            shift_start -= timedelta(days=1)
        return shift_start


@dataclass
class OrdersTelemetryState:
    first_event_wallclock_ts: str = ""
    first_event_monotonic: float = 0.0
    last_event_monotonic: float = 0.0
    last_summary_monotonic: float = 0.0
    dirty_events: int = 0
    sample_count: int = 0
    total_loads: int = 0
    total_update_loads: int = 0
    cache_hits: int = 0
    delta_applied: int = 0
    delta_skipped_stale: int = 0
    delta_failed: int = 0
    delta_skip_unsupported_scope_count: int = 0
    delta_fail_merge_error_count: int = 0
    delta_fail_other_count: int = 0
    fallback_after_delta: int = 0
    delta_time_ms_total: float = 0.0
    delta_time_samples: int = 0
    full_reload_count: int = 0
    full_reload_time_ms_total: float = 0.0
    user_open_count: int = 0
    user_open_time_ms_total: float = 0.0
    refresh_load_count: int = 0
    duplicate_load_prevented: int = 0
    race_reject_count: int = 0
    stale_apply_blocked: int = 0


class ChangeLogApplier:
    """Best-effort delta applier for cached snapshots. Must stay conservative."""

    SAFE_ORDERS_DELTA_ENTITIES = {"administrations"}

    def __init__(self, remcard_service):
        self.remcard_service = remcard_service

    def normalize_change_rows(self, rows: Any) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for row in rows or ():
            if row is None:
                continue
            if hasattr(row, "keys"):
                normalized.append(
                    {
                        "id": int(row["id"]) if row["id"] is not None else None,
                        "entity_name": str(row["entity_name"]) if row["entity_name"] is not None else None,
                        "entity_id": row["entity_id"],
                        "admission_id": row["admission_id"],
                        "action": row["action"],
                        "changed_at": row["changed_at"],
                        "changed_by": row["changed_by"],
                        "version": row["version"],
                    }
                )
            else:
                normalized.append(
                    {
                        "id": int(row[0]) if len(row) > 0 and row[0] is not None else None,
                        "entity_name": str(row[1]) if len(row) > 1 and row[1] is not None else None,
                        "entity_id": row[2] if len(row) > 2 else None,
                        "admission_id": row[3] if len(row) > 3 else None,
                        "action": row[4] if len(row) > 4 else None,
                        "changed_at": row[5] if len(row) > 5 else None,
                        "changed_by": row[6] if len(row) > 6 else None,
                        "version": row[7] if len(row) > 7 else None,
                    }
                )
        return normalized

    def get_orders_delta_plan(
        self,
        *,
        base_snapshot,
        change_rows: Any,
    ) -> Dict[str, Any]:
        rows = self.normalize_change_rows(change_rows)
        base_version = int((base_snapshot or {}).get("version") or 0)
        if not rows:
            return {
                "apply": False,
                "reason": "empty_change_rows",
                "latest_change_id": base_version,
                "rows": [],
                "changed_entities": set(),
            }
        changed_entities = {
            str(row.get("entity_name") or "")
            for row in rows
            if row.get("entity_name")
        }
        latest_change_id = max(
            [base_version]
            + [int(row.get("id") or 0) for row in rows]
        )
        unsupported_entities = changed_entities - self.SAFE_ORDERS_DELTA_ENTITIES
        if unsupported_entities:
            return {
                "apply": False,
                "reason": f"unsupported_entities:{','.join(sorted(unsupported_entities))}",
                "latest_change_id": latest_change_id,
                "rows": rows,
                "changed_entities": changed_entities,
            }
        return {
            "apply": True,
            "reason": "administrations_only",
            "latest_change_id": latest_change_id,
            "rows": rows,
            "changed_entities": changed_entities,
        }

    def apply_orders_delta(
        self,
        *,
        context: OrdersContext,
        base_snapshot,
        latest_change_id: int,
    ) -> Dict[str, Any]:
        admin_cursor = (base_snapshot or {}).get("orders_admin_cursor") or make_sync_cursor(EPOCH_SYNC_TS, 0)
        delta_rows = self.remcard_service.get_latest_administrations(
            admission_id=context.admission_id,
            shift_date=context.shift_date,
            only_committed=(context.variant == "committed"),
            include_deleted=True,
            include_cancelled=True,
            include_deleted_orders=True,
            updated_after=admin_cursor,
        )
        if not delta_rows:
            raise ValueError("empty_delta_rows")

        merged_rows = self._merge_admin_rows(
            base_snapshot=base_snapshot,
            delta_rows=delta_rows,
        )
        updated_snapshot = dict(base_snapshot or {})
        updated_snapshot["admin_rows"] = merged_rows
        updated_snapshot["has_any_draft"] = self._has_any_draft(
            orders=updated_snapshot.get("orders") or (),
            admin_rows=merged_rows,
        )
        updated_snapshot["has_any_administrations"] = any(
            str(row.get("status") or "") not in ("deleted", "cancelled")
            for row in merged_rows
        )
        updated_snapshot["has_any_orders"] = bool(updated_snapshot.get("orders"))
        updated_snapshot["change_id"] = int(latest_change_id)
        return updated_snapshot

    @staticmethod
    def _has_any_draft(*, orders, admin_rows) -> bool:
        return any(
            int(getattr(order, "is_committed", 0) or 0) == 0
            or getattr(order, "draft_sort_order", None) is not None
            for order in (orders or ())
            if order
        ) or any(
            int(dict(row).get("is_committed", 0) or 0) == 0
            for row in (admin_rows or ())
        )

    def _merge_admin_rows(self, *, base_snapshot, delta_rows) -> List[Dict[str, Any]]:
        visible_order_ids = {
            int(getattr(order, "id"))
            for order in (base_snapshot or {}).get("orders") or ()
            if getattr(order, "id", None) is not None
        }
        if not visible_order_ids:
            raise ValueError("delta_without_visible_orders")

        admin_map: Dict[Tuple[int, str], Dict[str, Any]] = {}
        for row in (base_snapshot or {}).get("admin_rows") or ():
            normalized = dict(row)
            key = self._admin_key(normalized)
            admin_map[key] = normalized

        changed = False
        for row in delta_rows:
            normalized = dict(row)
            order_id = int(normalized.get("order_id") or 0)
            if order_id not in visible_order_ids:
                raise ValueError(f"delta_unknown_order:{order_id}")
            key = self._admin_key(normalized)
            if admin_map.get(key) != normalized:
                admin_map[key] = normalized
                changed = True

        if not changed:
            raise ValueError("delta_no_effect")

        return sorted(
            admin_map.values(),
            key=lambda row: (str(row.get("planned_time") or ""), int(row.get("id") or 0)),
        )

    @staticmethod
    def _admin_key(row: Dict[str, Any]) -> Tuple[int, str]:
        return (
            int(row.get("order_id") or 0),
            str(row.get("planned_time") or ""),
        )


class ReadCoordinator:
    """Read-only orchestration layer for immutable snapshots and lightweight caching."""

    def __init__(
        self,
        remcard_service,
        *,
        max_cached_patients: int = READ_CACHE_MAX_PATIENTS,
        max_tabs_per_patient: int = READ_CACHE_MAX_TABS_PER_PATIENT,
        max_concurrent_loads: int = READ_MAX_CONCURRENT_LOADS,
    ):
        self.remcard_service = remcard_service
        self.max_cached_patients = max(1, int(max_cached_patients or 1))
        self.max_tabs_per_patient = max(1, int(max_tabs_per_patient or 1))
        self.max_concurrent_loads = max(1, int(max_concurrent_loads or 1))

        self._patient_vitals_cache: "OrderedDict[tuple[str, int, str, str, str, str, str], MappingProxyType]" = OrderedDict()
        self._patient_cache_index: dict[int, OrderedDict[str, None]] = {}
        self._patient_card_cache: "OrderedDict[tuple[str, int, str, str, str, str, str], MappingProxyType]" = OrderedDict()
        self._patient_card_cache_index: dict[int, OrderedDict[str, None]] = {}
        self._patient_scope_cache: "OrderedDict[tuple[str, int, str, str, str, str, str], MappingProxyType]" = OrderedDict()
        self._patient_scope_cache_index: dict[int, OrderedDict[str, None]] = {}
        self._cache_version_validation: dict[tuple, Tuple[int, int]] = {}

        self._orders_tab_cache: "OrderedDict[tuple[str, int, str, str, str, str, str], MappingProxyType]" = OrderedDict()
        self._orders_cache_index: dict[int, OrderedDict[str, None]] = {}
        self._orders_stale_versions: dict[tuple[str, int, str, str, str, str, str], int] = {}
        self._orders_stale_snapshots: dict[tuple[str, int, str, str, str, str, str], MappingProxyType] = {}
        self._change_log_applier = ChangeLogApplier(remcard_service)

        self._load_condition = threading.Condition()
        self._load_waiting: list[tuple[int, int, str, str, str, str]] = []
        self._load_counter = count(1)
        self._trace_counter = count(1)
        self._active_loads = 0
        self._orders_telemetry = OrdersTelemetryState()
        self._orders_telemetry_lock = threading.Lock()

    def make_orders_context(
        self,
        *,
        source_db: str,
        admission_id: int,
        shift_date: datetime,
        role: str,
        mode: str,
        variant: str,
    ) -> OrdersContext:
        return OrdersContext(
            source_db=source_db,
            admission_id=admission_id,
            shift_date=shift_date,
            role=role,
            mode=mode,
            variant=variant,
        )

    def make_patient_snapshot_context(
        self,
        *,
        source_db: str,
        admission_id: int,
        shift_date: datetime,
        role: str,
        mode: str,
        variant: str = "vitals",
    ) -> PatientSnapshotContext:
        return PatientSnapshotContext(
            source_db=source_db,
            admission_id=admission_id,
            shift_date=shift_date,
            role=role,
            mode=mode,
            variant=variant,
        )

    def load_patient_vitals_snapshot(
        self,
        admission_id: int,
        shift_date: datetime,
        *,
        role: str,
        mode: str = "live",
        source_db: Optional[str] = None,
        ensure_initial_status: bool = False,
        force_refresh: bool = True,
    ):
        context = self.make_patient_snapshot_context(
            source_db=source_db or "live",
            admission_id=admission_id,
            shift_date=shift_date,
            role=role,
            mode=mode,
            variant="vitals",
        )
        cache_key = context.cache_key()
        context_hash = context.hash()
        trace_id = self._next_trace_id("patient_vitals", context_hash)
        started = time.perf_counter()

        if context.mode == "live" and not force_refresh:
            cached = self.get_current_cached_vitals(cache_key)
            if cached is not None:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                logger.info(
                    "[ReadCoordinator] patient_vitals cache_hit=1 admission_id=%s role=%s mode=%s version=%s context_hash=%s trace_id=%s elapsed_ms=%.2f",
                    context.admission_id,
                    context.role,
                    context.mode,
                    cached.get("version"),
                    context_hash,
                    cached.get("load_trace_id") or trace_id,
                    elapsed_ms,
                )
                return cached

        load_strategy = "patient_vitals"
        fallback_used = 0
        try:
            snapshot = self.remcard_service.build_vitals_snapshot(
                context.admission_id,
                context.shift_date,
                include_change_cursor=True,
                ensure_initial_status=ensure_initial_status,
            )
        except Exception:
            load_strategy = "legacy_fallback"
            fallback_used = 1
            logger.exception(
                "[ReadCoordinator] coordinator_fail scope=patient_vitals admission_id=%s role=%s mode=%s context_hash=%s trace_id=%s reason=build_vitals_snapshot_failed",
                context.admission_id,
                context.role,
                context.mode,
                context_hash,
                trace_id,
            )
            snapshot = self.remcard_service.build_full_card_snapshot(
                context.admission_id,
                context.shift_date,
                include_change_cursor=True,
                include_balance=True,
                balance_only_committed=(context.role == "nurse"),
                ensure_initial_status=ensure_initial_status,
            )
            logger.warning(
                "[ReadCoordinator] fallback_to_legacy scope=patient_vitals admission_id=%s role=%s mode=%s context_hash=%s trace_id=%s reason=build_vitals_snapshot_failed",
                context.admission_id,
                context.role,
                context.mode,
                context_hash,
                trace_id,
            )

        frozen_snapshot = self._finalize_snapshot(
            snapshot=snapshot,
            scope="patient_vitals",
            tab_name="vitals",
            cache_key=cache_key,
            context_hash=context_hash,
            role=context.role,
            mode=context.mode,
            source_db=context.source_db,
            variant=context.variant,
            load_strategy=load_strategy,
            load_trace_id=trace_id,
            source="patient_open",
            stale=False,
            invalidate_reason=None,
        )

        if context.mode == "live":
            self._store_patient_vitals(context, frozen_snapshot)
            self._mark_cache_validated_by_monitor(cache_key)

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        logger.info(
            "[ReadCoordinator] patient_vitals loaded admission_id=%s role=%s mode=%s strategy=%s cache_hit=0 fallback=%s version=%s context_hash=%s trace_id=%s elapsed_ms=%.2f",
            context.admission_id,
            context.role,
            context.mode,
            load_strategy,
            fallback_used,
            frozen_snapshot.get("version"),
            context_hash,
            trace_id,
            elapsed_ms,
        )
        return frozen_snapshot

    def load_patient_card_snapshot(
        self,
        admission_id: int,
        shift_date: datetime,
        *,
        role: str,
        mode: str = "live",
        source_db: Optional[str] = None,
        ensure_initial_status: bool = False,
        balance_only_committed: bool = False,
        force_refresh: bool = True,
    ):
        context = self.make_patient_snapshot_context(
            source_db=source_db or "live",
            admission_id=admission_id,
            shift_date=shift_date,
            role=role,
            mode=mode,
            variant="card_committed" if balance_only_committed else "card_full",
        )
        cache_key = context.cache_key()
        context_hash = context.hash()
        trace_id = self._next_trace_id("patient_card", context_hash)
        started = time.perf_counter()

        if context.mode == "live" and not force_refresh:
            cached = self.get_current_cached_card(cache_key)
            if cached is not None:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                logger.info(
                    "[ReadCoordinator] patient_card cache_hit=1 admission_id=%s role=%s mode=%s version=%s context_hash=%s trace_id=%s elapsed_ms=%.2f",
                    context.admission_id,
                    context.role,
                    context.mode,
                    cached.get("version"),
                    context_hash,
                    cached.get("load_trace_id") or trace_id,
                    elapsed_ms,
                )
                return cached

        snapshot = self.remcard_service.build_full_card_snapshot(
            context.admission_id,
            context.shift_date,
            include_change_cursor=True,
            include_balance=True,
            balance_only_committed=balance_only_committed,
            ensure_initial_status=ensure_initial_status,
        )

        frozen_snapshot = self._finalize_snapshot(
            snapshot=snapshot,
            scope="patient_card",
            tab_name="card",
            cache_key=cache_key,
            context_hash=context_hash,
            role=context.role,
            mode=context.mode,
            source_db=context.source_db,
            variant=context.variant,
            load_strategy="patient_card",
            load_trace_id=trace_id,
            source="patient_open",
            stale=False,
            invalidate_reason=None,
        )

        if context.mode == "live":
            self._store_patient_card(context, frozen_snapshot)
            self._mark_cache_validated_by_monitor(cache_key)

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        logger.info(
            "[ReadCoordinator] patient_card loaded admission_id=%s role=%s mode=%s cache_hit=0 version=%s context_hash=%s trace_id=%s elapsed_ms=%.2f",
            context.admission_id,
            context.role,
            context.mode,
            frozen_snapshot.get("version"),
            context_hash,
            trace_id,
            elapsed_ms,
        )
        return frozen_snapshot

    def load_balance_snapshot(
        self,
        admission_id: int,
        shift_date: datetime,
        *,
        role: str,
        mode: str = "live",
        source_db: Optional[str] = None,
        balance_only_committed: bool = False,
        force_refresh: bool = True,
    ):
        return self._load_patient_scope_snapshot(
            admission_id=admission_id,
            shift_date=shift_date,
            role=role,
            mode=mode,
            source_db=source_db,
            scope="balance",
            variant="balance_committed" if balance_only_committed else "balance_full",
            force_refresh=force_refresh,
            build_snapshot=lambda context: self.remcard_service.build_balance_snapshot(
                context.admission_id,
                context.shift_date,
                include_change_cursor=True,
                balance_only_committed=balance_only_committed,
            ),
        )

    def load_diet_snapshot(
        self,
        admission_id: int,
        shift_date: datetime,
        *,
        role: str,
        mode: str = "live",
        source_db: Optional[str] = None,
        force_refresh: bool = True,
    ):
        return self._load_patient_scope_snapshot(
            admission_id=admission_id,
            shift_date=shift_date,
            role=role,
            mode=mode,
            source_db=source_db,
            scope="diet",
            force_refresh=force_refresh,
            build_snapshot=lambda context: self.remcard_service.build_diet_snapshot(
                context.admission_id,
                context.shift_date,
                include_change_cursor=True,
            ),
        )

    def load_patient_header_snapshot(
        self,
        admission_id: int,
        shift_date: datetime,
        *,
        role: str,
        mode: str = "live",
        source_db: Optional[str] = None,
        force_refresh: bool = True,
    ):
        return self._load_patient_scope_snapshot(
            admission_id=admission_id,
            shift_date=shift_date,
            role=role,
            mode=mode,
            source_db=source_db,
            scope="patient_header",
            force_refresh=force_refresh,
            build_snapshot=lambda context: self.remcard_service.build_patient_header_snapshot(
                context.admission_id,
                context.shift_date,
                include_change_cursor=True,
            ),
        )

    def load_status_snapshot(
        self,
        admission_id: int,
        shift_date: datetime,
        *,
        role: str,
        mode: str = "live",
        source_db: Optional[str] = None,
        force_refresh: bool = True,
    ):
        return self._load_patient_scope_snapshot(
            admission_id=admission_id,
            shift_date=shift_date,
            role=role,
            mode=mode,
            source_db=source_db,
            scope="status",
            force_refresh=force_refresh,
            build_snapshot=lambda context: self.remcard_service.build_status_snapshot(
                context.admission_id,
                context.shift_date,
                include_change_cursor=True,
            ),
        )

    def load_ivl_snapshot(
        self,
        admission_id: int,
        shift_date: datetime,
        *,
        role: str,
        mode: str = "live",
        source_db: Optional[str] = None,
        force_refresh: bool = True,
    ):
        return self._load_patient_scope_snapshot(
            admission_id=admission_id,
            shift_date=shift_date,
            role=role,
            mode=mode,
            source_db=source_db,
            scope="ivl",
            force_refresh=force_refresh,
            build_snapshot=lambda context: self.remcard_service.build_ivl_snapshot(
                context.admission_id,
                context.shift_date,
                include_change_cursor=True,
            ),
        )

    def load_beds_snapshot(
        self,
        reference_dt: Optional[datetime] = None,
        *,
        role: str,
        mode: str = "live",
        source_db: Optional[str] = None,
        force_refresh: bool = True,
    ):
        reference_dt = reference_dt or datetime.now()
        return self._load_patient_scope_snapshot(
            admission_id=0,
            shift_date=reference_dt,
            role=role,
            mode=mode,
            source_db=source_db,
            scope="beds",
            force_refresh=force_refresh,
            build_snapshot=lambda _context: self.remcard_service.build_beds_snapshot(
                reference_dt=reference_dt,
                include_change_cursor=True,
            ),
        )

    def _load_patient_scope_snapshot(
        self,
        *,
        admission_id: int,
        shift_date: datetime,
        role: str,
        scope: str,
        build_snapshot,
        mode: str = "live",
        source_db: Optional[str] = None,
        variant: Optional[str] = None,
        force_refresh: bool = True,
    ):
        context = self.make_patient_snapshot_context(
            source_db=source_db or "live",
            admission_id=admission_id,
            shift_date=shift_date,
            role=role,
            mode=mode,
            variant=variant or scope,
        )
        cache_key = context.cache_key()
        context_hash = context.hash()
        trace_id = self._next_trace_id(scope, context_hash)
        started = time.perf_counter()

        if context.mode == "live" and not force_refresh:
            cached = self.get_current_cached_patient_scope(cache_key)
            if cached is not None:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                logger.info(
                    "[ReadCoordinator] partial_scope cache_hit=1 scope=%s admission_id=%s role=%s mode=%s version=%s context_hash=%s trace_id=%s elapsed_ms=%.2f",
                    scope,
                    context.admission_id,
                    context.role,
                    context.mode,
                    cached.get("version"),
                    context_hash,
                    cached.get("load_trace_id") or trace_id,
                    elapsed_ms,
                )
                return cached

        snapshot = build_snapshot(context)
        frozen_snapshot = self._finalize_snapshot(
            snapshot=snapshot,
            scope=scope,
            tab_name=scope,
            cache_key=cache_key,
            context_hash=context_hash,
            role=context.role,
            mode=context.mode,
            source_db=context.source_db,
            variant=context.variant,
            load_strategy=f"{scope}_snapshot",
            load_trace_id=trace_id,
            source="partial_refresh",
            stale=False,
            invalidate_reason=None,
        )

        if context.mode == "live":
            self._store_patient_scope(context, frozen_snapshot)
            self._mark_cache_validated_by_monitor(cache_key)

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        logger.info(
            "[ReadCoordinator] partial_scope loaded scope=%s admission_id=%s role=%s mode=%s cache_hit=0 version=%s content_hash=%s context_hash=%s trace_id=%s elapsed_ms=%.2f",
            scope,
            context.admission_id,
            context.role,
            context.mode,
            frozen_snapshot.get("version"),
            frozen_snapshot.get("content_hash"),
            context_hash,
            trace_id,
            elapsed_ms,
        )
        return frozen_snapshot

    def load_orders_tab(
        self,
        context: OrdersContext,
        *,
        source: str = "refresh",
        priority: str = "HIGH",
        force_refresh: bool = False,
        timeout_sec: float = READ_LOAD_TIMEOUT_SEC,
        _retry: bool = False,
    ):
        context_hash = context.hash()
        with foreground_read(
            "orders",
            admission_id=context.admission_id,
            source=source,
            context_hash=context_hash,
            priority=self._normalize_priority(priority),
        ):
            return self._load_orders_tab_impl(
                context,
                source=source,
                priority=priority,
                force_refresh=force_refresh,
                timeout_sec=timeout_sec,
                _retry=_retry,
            )

    def _load_orders_tab_impl(
        self,
        context: OrdersContext,
        *,
        source: str = "refresh",
        priority: str = "HIGH",
        force_refresh: bool = False,
        timeout_sec: float = READ_LOAD_TIMEOUT_SEC,
        _retry: bool = False,
    ):
        context_hash = context.hash()
        trace_id = self._next_trace_id("orders", context_hash)
        started = time.perf_counter()
        cache_key = context.cache_key()
        delta_attempted = False
        delta_applied = 0
        delta_failure_reason = "0"
        delta_time_ms = 0.0
        delta_base = "none"
        base_snapshot = None

        if context.mode == "live" and not force_refresh:
            cached = self.get_cached_tab(context)
            if cached is not None:
                base_snapshot = cached
                delta_base = "cache"
            else:
                base_snapshot = self._orders_stale_snapshots.get(cache_key)
                if base_snapshot is not None:
                    delta_base = "stale_base"
                    logger.info(
                        "[ReadCoordinator] orders_cache_lookup hit=0 stale_base=1 context_hash=%s trace_id=%s",
                        context_hash,
                        base_snapshot.get("load_trace_id") or trace_id,
                    )

        if base_snapshot is not None and not force_refresh:
            current_change_id = self._get_orders_current_change_id(context)
            base_version = int(base_snapshot.get("version") or 0)
            if current_change_id <= base_version:
                if context.mode == "live" and delta_base != "cache":
                    self._store_orders_tab(context, base_snapshot)
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                logger.info(
                    "[ReadCoordinator] orders_load_time_ms=%.2f orders_cache_hit=1 orders_source=%s orders_fallback=0 orders_delta_applied=0 orders_delta_failed=0 orders_delta_time_ms=0.00 orders_fallback_after_delta=0 context_hash=%s trace_id=%s priority=%s base=%s version=%s",
                    elapsed_ms,
                    source,
                    context_hash,
                    base_snapshot.get("load_trace_id") or trace_id,
                    self._normalize_priority(priority),
                    delta_base,
                    base_snapshot.get("version"),
                )
                self._record_orders_load_telemetry(
                    source=source,
                    cache_hit=True,
                    delta_applied=False,
                    delta_failure_reason="0",
                    delta_time_ms=0.0,
                    elapsed_ms=elapsed_ms,
                    fallback_after_delta=False,
                    admission_id=context.admission_id,
                    context_hash=context_hash,
                    trace_id=base_snapshot.get("load_trace_id") or trace_id,
                    priority=priority,
                    fallback_used=0,
                    base=delta_base,
                    version=int(base_snapshot.get("version") or 0),
                )
                return base_snapshot

            stale_age_sec = self._snapshot_age_sec(base_snapshot)
            if (
                delta_base == "stale_base"
                and stale_age_sec is not None
                and stale_age_sec > READ_ORDERS_DELTA_STALE_THRESHOLD_SEC
            ):
                delta_attempted = True
                delta_failure_reason = f"stale_base_age_exceeded:{stale_age_sec:.2f}"
                logger.info(
                    "[ReadCoordinator] orders_delta_skip reason=%s threshold_sec=%.2f context_hash=%s trace_id=%s priority=%s base=%s",
                    delta_failure_reason,
                    READ_ORDERS_DELTA_STALE_THRESHOLD_SEC,
                    context_hash,
                    trace_id,
                    self._normalize_priority(priority),
                    delta_base,
                )
            else:
                delta_attempted = True
                delta_started = time.perf_counter()
                try:
                    delta_snapshot = self._try_apply_orders_delta(
                        context=context,
                        base_snapshot=base_snapshot,
                        current_change_id=current_change_id,
                        source=source,
                        trace_id=trace_id,
                    )
                    delta_time_ms = (time.perf_counter() - delta_started) * 1000.0
                    delta_applied = 1
                    elapsed_ms = (time.perf_counter() - started) * 1000.0
                    logger.info(
                        "[ReadCoordinator] orders_load_time_ms=%.2f orders_cache_hit=1 orders_source=%s orders_fallback=0 orders_delta_applied=1 orders_delta_failed=0 orders_delta_time_ms=%.2f orders_fallback_after_delta=0 context_hash=%s trace_id=%s priority=%s base=%s version=%s",
                        elapsed_ms,
                        source,
                        delta_time_ms,
                        context_hash,
                        delta_snapshot.get("load_trace_id") or trace_id,
                        self._normalize_priority(priority),
                        delta_base,
                        delta_snapshot.get("version"),
                    )
                    self._record_orders_load_telemetry(
                        source=source,
                        cache_hit=True,
                        delta_applied=True,
                        delta_failure_reason="0",
                        delta_time_ms=delta_time_ms,
                        elapsed_ms=elapsed_ms,
                        fallback_after_delta=False,
                        admission_id=context.admission_id,
                        context_hash=context_hash,
                        trace_id=delta_snapshot.get("load_trace_id") or trace_id,
                        priority=priority,
                        fallback_used=0,
                        base=delta_base,
                        version=int(delta_snapshot.get("version") or 0),
                    )
                    return delta_snapshot
                except Exception as exc:
                    delta_time_ms = (time.perf_counter() - delta_started) * 1000.0
                    delta_failure_reason = str(exc) or exc.__class__.__name__
                    logger.log(
                        self._orders_delta_fallback_log_level(delta_failure_reason),
                        "[ReadCoordinator] orders_delta_applied=0 orders_delta_failed=%s orders_delta_time_ms=%.2f orders_fallback_after_delta=1 context_hash=%s trace_id=%s priority=%s base=%s",
                        delta_failure_reason,
                        delta_time_ms,
                        context_hash,
                        trace_id,
                        self._normalize_priority(priority),
                        delta_base,
                    )

        fallback_used = 0
        stale_version = int(self._orders_stale_versions.get(cache_key) or 0)
        stale_snapshot_for_validation = self._orders_stale_snapshots.get(cache_key)
        with self._load_slot(
            priority=priority,
            scope="orders",
            context_hash=context_hash,
            source=source,
            timeout_sec=timeout_sec,
            trace_id=trace_id,
        ):
            try:
                build_started = time.perf_counter()
                with orders_snapshot_caller("read_coordinator", context_hash=context_hash, request_source=source):
                    snapshot = self.remcard_service.build_orders_snapshot(
                        context.admission_id,
                        context.shift_date,
                        only_committed=(context.variant == "committed"),
                        include_change_cursor=True,
                    )
                build_elapsed_ms = (time.perf_counter() - build_started) * 1000.0
                logger.info(
                    "[ReadCoordinator] build_orders_snapshot_time_ms=%.2f path=read_coordinator admission_id=%s source=%s context_hash=%s trace_id=%s",
                    build_elapsed_ms,
                    context.admission_id,
                    source,
                    context_hash,
                    trace_id,
                )
                record_metric(
                    "build_orders_snapshot_time_ms",
                    round(build_elapsed_ms, 3),
                    admission_id=context.admission_id,
                    source="click" if str(source or "").lower() == "user" else "refresh",
                    path="read_coordinator",
                    context_hash=context_hash,
                    trace_id=trace_id,
                    status="ok",
                )
            except Exception:
                failed_elapsed_ms = (time.perf_counter() - build_started) * 1000.0 if "build_started" in locals() else 0.0
                record_metric(
                    "build_orders_snapshot_time_ms",
                    round(failed_elapsed_ms, 3),
                    admission_id=context.admission_id,
                    source="click" if str(source or "").lower() == "user" else "refresh",
                    path="read_coordinator",
                    context_hash=context_hash,
                    trace_id=trace_id,
                    status="error",
                )
                fallback_used = 1
                logger.exception(
                    "[ReadCoordinator] coordinator_fail scope=orders admission_id=%s source=%s priority=%s context_hash=%s trace_id=%s reason=build_orders_snapshot_failed",
                    context.admission_id,
                    source,
                    self._normalize_priority(priority),
                    context_hash,
                    trace_id,
                )
                fallback_started = time.perf_counter()
                with orders_snapshot_caller("legacy_fallback", context_hash=context_hash, request_source=source):
                    snapshot = self.remcard_service.build_orders_snapshot(
                        context.admission_id,
                        context.shift_date,
                        only_committed=(context.variant == "committed"),
                        include_change_cursor=True,
                    )
                fallback_elapsed_ms = (time.perf_counter() - fallback_started) * 1000.0
                record_metric(
                    "build_orders_snapshot_time_ms",
                    round(fallback_elapsed_ms, 3),
                    admission_id=context.admission_id,
                    source="refresh",
                    path="legacy_fallback",
                    context_hash=context_hash,
                    trace_id=trace_id,
                    status="ok",
                )
                logger.warning(
                    "[ReadCoordinator] fallback_to_legacy scope=orders admission_id=%s source=%s priority=%s context_hash=%s trace_id=%s reason=build_orders_snapshot_failed",
                    context.admission_id,
                    source,
                    self._normalize_priority(priority),
                    context_hash,
                    trace_id,
            )

        frozen_snapshot = self._finalize_snapshot(
            snapshot=snapshot,
            scope="orders_tab",
            tab_name="orders",
            cache_key=cache_key,
            context_hash=context_hash,
            role=context.role,
            mode=context.mode,
            source_db=context.source_db,
            variant=context.variant,
            load_strategy="orders_snapshot",
            load_trace_id=trace_id,
            source=source,
            stale=False,
            invalidate_reason=None,
        )

        if stale_version > 0:
            new_version = int(frozen_snapshot.get("version") or 0)
            if new_version <= stale_version:
                if self._orders_snapshot_content_changed(stale_snapshot_for_validation, frozen_snapshot):
                    logger.warning(
                        "[ReadCoordinator] version_violation_content_changed scope=orders admission_id=%s old_version=%s new_version=%s context_hash=%s trace_id=%s action=accept_snapshot",
                        context.admission_id,
                        stale_version,
                        new_version,
                        context_hash,
                        trace_id,
                    )
                else:
                    logger.info(
                        "[ReadCoordinator] version_unchanged scope=orders admission_id=%s old_version=%s new_version=%s context_hash=%s trace_id=%s action=accept_snapshot",
                        context.admission_id,
                        stale_version,
                        new_version,
                        context_hash,
                        trace_id,
                    )

        self._orders_stale_versions.pop(cache_key, None)
        if context.mode == "live":
            self._store_orders_tab(context, frozen_snapshot)

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        logger.info(
            "[ReadCoordinator] orders_load_time_ms=%.2f orders_cache_hit=0 orders_source=%s orders_fallback=%s orders_delta_applied=%s orders_delta_failed=%s orders_delta_time_ms=%.2f orders_fallback_after_delta=%s context_hash=%s trace_id=%s priority=%s version=%s",
            elapsed_ms,
            source,
            fallback_used,
            delta_applied,
            delta_failure_reason,
            delta_time_ms,
            int(delta_attempted and not delta_applied),
            context_hash,
            trace_id,
            self._normalize_priority(priority),
            frozen_snapshot.get("version"),
        )
        self._record_orders_load_telemetry(
            source=source,
            cache_hit=False,
            delta_applied=bool(delta_applied),
            delta_failure_reason=delta_failure_reason,
            delta_time_ms=delta_time_ms,
            elapsed_ms=elapsed_ms,
            fallback_after_delta=bool(delta_attempted and not delta_applied),
            admission_id=context.admission_id,
            context_hash=context_hash,
            trace_id=trace_id,
            priority=priority,
            fallback_used=fallback_used,
            base="fresh",
            version=int(frozen_snapshot.get("version") or 0),
        )
        return frozen_snapshot

    def get_cached_tab(self, context: OrdersContext, *, allow_stale: bool = False):
        cache_key = context.cache_key()
        snapshot = self._orders_tab_cache.get(cache_key)
        if snapshot is None:
            logger.info(
                "[ReadCoordinator] orders_cache_lookup hit=0 stale=0 context_hash=%s",
                context.hash(),
            )
            return None
        self._orders_tab_cache.move_to_end(cache_key)
        is_stale = bool(snapshot.get("stale"))
        logger.info(
            "[ReadCoordinator] orders_cache_lookup hit=1 stale=%s context_hash=%s trace_id=%s",
            int(is_stale),
            context.hash(),
            snapshot.get("load_trace_id"),
        )
        if is_stale and not allow_stale:
            return None
        return snapshot

    def invalidate_tab(self, context: OrdersContext, reason: str) -> None:
        cache_key = context.cache_key()
        snapshot = self._orders_tab_cache.pop(cache_key, None)
        if snapshot is not None:
            self._orders_stale_snapshots[cache_key] = snapshot
        old_version = int((snapshot or {}).get("version") or self._orders_stale_versions.get(cache_key) or 0)
        if old_version > 0:
            self._orders_stale_versions[cache_key] = old_version
        self._drop_orders_cache_index(context)
        logger.info(
            "[ReadCoordinator] orders_invalidate reason=%s old_version=%s context_hash=%s cache_present=%s stale_base_saved=%s",
            str(reason or "unknown"),
            old_version,
            context.hash(),
            int(snapshot is not None),
            int(snapshot is not None),
        )

    def record_orders_ui_event(self, event_name: str, *, role: str, context_hash: Optional[str] = None) -> None:
        event = str(event_name or "").strip().lower()
        normalized_role = str(role or "").strip().lower() or "unknown"
        now = time.monotonic()
        with self._orders_telemetry_lock:
            state = self._orders_telemetry
            if state.first_event_monotonic <= 0:
                state.first_event_monotonic = now
                state.first_event_wallclock_ts = datetime.now().isoformat(timespec="seconds")
            state.last_event_monotonic = now
            state.dirty_events += 1
            state.sample_count += 1
            if event == "duplicate_load_prevented":
                state.duplicate_load_prevented += 1
            elif event == "race_reject":
                state.race_reject_count += 1
            elif event == "stale_apply_blocked":
                state.stale_apply_blocked += 1
            self._maybe_log_orders_telemetry_summary_locked()
        logger.info(
            "[ReadCoordinator] orders_ui_event=%s role=%s context_hash=%s",
            event or "unknown",
            normalized_role,
            context_hash or "unknown",
        )

    def get_orders_telemetry_snapshot(self) -> Dict[str, Any]:
        with self._orders_telemetry_lock:
            return self._build_orders_telemetry_snapshot_locked()

    def _get_observed_change_state(self) -> Optional[Tuple[int, int]]:
        getter = getattr(self.remcard_service, "get_observed_change_state", None)
        if not callable(getter):
            return None
        try:
            state = getter()
            if not state:
                return None
            change_id = int(state.get("change_id") or 0)
            state_epoch = int(state.get("state_epoch") or 0)
            refresh_request_seq = int(state.get("refresh_request_seq") or 0)
            refresh_observed_seq = int(state.get("refresh_observed_seq") or 0)
            observed_monotonic = float(state.get("observed_monotonic") or 0.0)
        except Exception as exc:
            logger.debug("[ReadCoordinator] observed_change_state_unavailable error=%s", exc)
            return None
        if observed_monotonic <= 0:
            return None
        if (time.monotonic() - observed_monotonic) > READ_MONITOR_STATE_MAX_AGE_SEC:
            return None
        if refresh_request_seq != refresh_observed_seq:
            return None
        return state_epoch, change_id

    def _cache_validation_covers_monitor(self, cache_key) -> Tuple[bool, Optional[int]]:
        observed = self._get_observed_change_state()
        if observed is None:
            return False, None
        validated = self._cache_version_validation.get(cache_key)
        if not validated:
            return False, observed[1]
        return (
            int(validated[0]) == int(observed[0])
            and int(validated[1]) >= int(observed[1]),
            observed[1],
        )

    def _mark_cache_validated_by_monitor(self, cache_key) -> None:
        observed = self._get_observed_change_state()
        if observed is not None:
            self._cache_version_validation[cache_key] = observed

    def _drop_cache_validation(self, cache_key) -> None:
        self._cache_version_validation.pop(cache_key, None)

    def get_cached_vitals(self, cache_key):
        snapshot = self._patient_vitals_cache.get(cache_key)
        if snapshot is None:
            persisted = persistent_snapshot_cache.load_snapshot("patient_vitals", cache_key)
            if persisted is None:
                return None
            snapshot = MappingProxyType(dict(persisted or {}))
            self._store_patient_vitals_by_key(cache_key, snapshot, persist=False)
            logger.info(
                "[ReadCoordinator] patient_vitals persistent_cache_hit=1 admission_id=%s version=%s",
                cache_key[1] if len(cache_key) > 1 else "unknown",
                snapshot.get("version"),
            )
            return snapshot
        self._patient_vitals_cache.move_to_end(cache_key)
        return snapshot

    def get_current_cached_vitals(self, cache_key):
        snapshot = self.get_cached_vitals(cache_key)
        if snapshot is None:
            return None

        try:
            admission_id = int(cache_key[1])
            cached_version = int(snapshot.get("version") or 0)
            monitor_current, observed_change_id = self._cache_validation_covers_monitor(cache_key)
            if monitor_current:
                logger.info(
                    "[ReadCoordinator] patient_vitals cache_current=1 admission_id=%s version=%s source=monitor observed_change_id=%s",
                    admission_id,
                    cached_version,
                    observed_change_id,
                )
                return snapshot
            current_version = int(
                self.remcard_service.get_latest_change_id(admission_id=admission_id) or 0
            )
        except Exception as exc:
            logger.warning(
                "[ReadCoordinator] patient_vitals cache_version_check_failed key=%s error=%s",
                cache_key,
                exc,
            )
            return None

        if current_version <= cached_version:
            self._mark_cache_validated_by_monitor(cache_key)
            logger.info(
                "[ReadCoordinator] patient_vitals cache_current=1 admission_id=%s version=%s source=db",
                admission_id,
                cached_version,
            )
            return snapshot

        self._drop_cache_validation(cache_key)
        logger.info(
            "[ReadCoordinator] patient_vitals cache_stale=1 admission_id=%s cached_version=%s current_version=%s action=preserve_for_swr",
            admission_id,
            cached_version,
            current_version,
        )
        return None

    def get_cached_card(self, cache_key):
        snapshot = self._patient_card_cache.get(cache_key)
        if snapshot is None:
            persisted = persistent_snapshot_cache.load_snapshot("patient_card", cache_key)
            if persisted is None:
                return None
            snapshot = MappingProxyType(dict(persisted or {}))
            self._store_patient_card_by_key(cache_key, snapshot, persist=False)
            logger.info(
                "[ReadCoordinator] patient_card persistent_cache_hit=1 admission_id=%s version=%s",
                cache_key[1] if len(cache_key) > 1 else "unknown",
                snapshot.get("version"),
            )
            return snapshot
        self._patient_card_cache.move_to_end(cache_key)
        return snapshot

    def get_current_cached_card(self, cache_key):
        snapshot = self.get_cached_card(cache_key)
        if snapshot is None:
            return None

        try:
            admission_id = int(cache_key[1])
            cached_version = int(snapshot.get("version") or 0)
            monitor_current, observed_change_id = self._cache_validation_covers_monitor(cache_key)
            if monitor_current:
                logger.info(
                    "[ReadCoordinator] patient_card cache_current=1 admission_id=%s version=%s source=monitor observed_change_id=%s",
                    admission_id,
                    cached_version,
                    observed_change_id,
                )
                return snapshot
            current_version = int(
                self.remcard_service.get_latest_change_id(admission_id=admission_id) or 0
            )
        except Exception as exc:
            logger.warning(
                "[ReadCoordinator] patient_card cache_version_check_failed key=%s error=%s",
                cache_key,
                exc,
            )
            return None

        if current_version <= cached_version:
            self._mark_cache_validated_by_monitor(cache_key)
            logger.info(
                "[ReadCoordinator] patient_card cache_current=1 admission_id=%s version=%s source=db",
                admission_id,
                cached_version,
            )
            return snapshot

        self._drop_cache_validation(cache_key)
        logger.info(
            "[ReadCoordinator] patient_card cache_stale=1 admission_id=%s cached_version=%s current_version=%s action=preserve_for_swr",
            admission_id,
            cached_version,
            current_version,
        )
        return None

    def get_cached_patient_scope(self, cache_key):
        snapshot = self._patient_scope_cache.get(cache_key)
        if snapshot is None:
            persisted = persistent_snapshot_cache.load_snapshot("patient_scope", cache_key)
            if persisted is None:
                return None
            snapshot = MappingProxyType(dict(persisted or {}))
            self._store_patient_scope_by_key(cache_key, snapshot, persist=False)
            logger.info(
                "[ReadCoordinator] patient_scope persistent_cache_hit=1 admission_id=%s scope=%s version=%s",
                cache_key[1] if len(cache_key) > 1 else "unknown",
                snapshot.get("scope"),
                snapshot.get("version"),
            )
            return snapshot
        self._patient_scope_cache.move_to_end(cache_key)
        return snapshot

    def get_current_cached_patient_scope(self, cache_key):
        snapshot = self.get_cached_patient_scope(cache_key)
        if snapshot is None:
            return None

        try:
            admission_id = int(cache_key[1])
            cached_version = int(snapshot.get("version") or 0)
            monitor_current, observed_change_id = self._cache_validation_covers_monitor(cache_key)
            if monitor_current:
                logger.info(
                    "[ReadCoordinator] patient_scope cache_current=1 admission_id=%s scope=%s version=%s source=monitor observed_change_id=%s",
                    admission_id,
                    snapshot.get("scope"),
                    cached_version,
                    observed_change_id,
                )
                return snapshot
            current_version = int(
                self.remcard_service.get_latest_change_id(
                    admission_id=(admission_id if admission_id > 0 else None),
                    include_global=True,
                )
                or 0
            )
        except Exception as exc:
            logger.warning(
                "[ReadCoordinator] patient_scope cache_version_check_failed key=%s error=%s",
                cache_key,
                exc,
            )
            return None

        if current_version <= cached_version:
            self._mark_cache_validated_by_monitor(cache_key)
            logger.info(
                "[ReadCoordinator] patient_scope cache_current=1 admission_id=%s scope=%s version=%s source=db",
                admission_id,
                snapshot.get("scope"),
                cached_version,
            )
            return snapshot

        self._drop_cache_validation(cache_key)
        logger.info(
            "[ReadCoordinator] patient_scope cache_stale=1 admission_id=%s scope=%s cached_version=%s current_version=%s action=preserve_for_swr",
            admission_id,
            snapshot.get("scope"),
            cached_version,
            current_version,
        )
        return None

    def invalidate_patient_vitals(
        self,
        *,
        source_db: str,
        admission_id: int,
        shift_date: datetime,
        role: str,
        mode: str,
    ) -> None:
        context = self.make_patient_snapshot_context(
            source_db=source_db,
            admission_id=admission_id,
            shift_date=shift_date,
            role=role,
            mode=mode,
            variant="vitals",
        )
        cache_key = context.cache_key()
        if cache_key in self._patient_vitals_cache:
            self._patient_vitals_cache.pop(cache_key, None)
            self._drop_cache_validation(cache_key)
            self._drop_patient_cache_index(context)
            logger.info(
                "[ReadCoordinator] invalidated patient_vitals cache key=%s context_hash=%s",
                cache_key,
                context.hash(),
            )

    def invalidate_patient_vitals_for_admission(self, admission_id: int, *, reason: str = "") -> int:
        if admission_id is None:
            return 0
        target_admission_id = int(admission_id)
        removed = 0
        for cache_key in list(self._patient_vitals_cache.keys()):
            if int(cache_key[1]) != target_admission_id:
                continue
            self._patient_vitals_cache.pop(cache_key, None)
            self._drop_cache_validation(cache_key)
            self._drop_cache_index_by_key(self._patient_cache_index, cache_key)
            removed += 1
        if removed:
            logger.info(
                "[ReadCoordinator] invalidated patient_vitals admission_id=%s entries=%s reason=%s",
                target_admission_id,
                removed,
                reason or "unknown",
            )
        return removed

    def invalidate_patient_card_for_admission(self, admission_id: int, *, reason: str = "") -> int:
        if admission_id is None:
            return 0
        target_admission_id = int(admission_id)
        removed = 0
        for cache_key in list(self._patient_card_cache.keys()):
            if int(cache_key[1]) != target_admission_id:
                continue
            self._patient_card_cache.pop(cache_key, None)
            self._drop_cache_validation(cache_key)
            self._drop_cache_index_by_key(self._patient_card_cache_index, cache_key)
            removed += 1
        if removed:
            logger.info(
                "[ReadCoordinator] invalidated patient_card admission_id=%s entries=%s reason=%s",
                target_admission_id,
                removed,
                reason or "unknown",
            )
        return removed

    def validate_reload_version(
        self,
        *,
        context: SnapshotContext,
        previous_snapshot,
        new_snapshot,
        scope: str,
    ) -> bool:
        old_version = int((previous_snapshot or {}).get("version") or 0)
        new_version = int((new_snapshot or {}).get("version") or 0)
        if old_version > 0 and new_version <= old_version:
            logger.error(
                "[ReadCoordinator] version_violation scope=%s admission_id=%s old_version=%s new_version=%s context_hash=%s",
                scope,
                context.admission_id,
                old_version,
                new_version,
                context.hash(),
            )
            return False
        return True

    def _get_orders_current_change_id(self, context: OrdersContext) -> int:
        return int(
            self.remcard_service.get_latest_change_id(
                admission_id=context.admission_id,
                include_global=False,
            )
            or 0
        )

    def _try_apply_orders_delta(
        self,
        *,
        context: OrdersContext,
        base_snapshot,
        current_change_id: int,
        source: str,
        trace_id: str,
    ):
        change_rows = self.remcard_service.get_changes_since(
            int((base_snapshot or {}).get("version") or 0),
            admission_id=context.admission_id,
            include_global=False,
        )
        delta_plan = self._change_log_applier.get_orders_delta_plan(
            base_snapshot=base_snapshot,
            change_rows=change_rows,
        )
        if not delta_plan.get("apply"):
            raise ValueError(delta_plan.get("reason") or "delta_not_safe")

        updated_snapshot = self._change_log_applier.apply_orders_delta(
            context=context,
            base_snapshot=base_snapshot,
            latest_change_id=int(current_change_id or delta_plan.get("latest_change_id") or 0),
        )
        delta_snapshot = self._finalize_snapshot(
            snapshot=updated_snapshot,
            scope="orders_tab",
            tab_name="orders",
            cache_key=context.cache_key(),
            context_hash=context.hash(),
            role=context.role,
            mode=context.mode,
            source_db=context.source_db,
            variant=context.variant,
            load_strategy="orders_delta",
            load_trace_id=trace_id,
            source=source,
            stale=False,
            invalidate_reason=None,
        )
        if not self.validate_reload_version(
            context=context,
            previous_snapshot=base_snapshot,
            new_snapshot=delta_snapshot,
            scope="orders_delta",
        ):
            raise ValueError("version_violation_after_delta")

        self._orders_stale_versions.pop(context.cache_key(), None)
        if context.mode == "live":
            self._store_orders_tab(context, delta_snapshot)
        return delta_snapshot

    @staticmethod
    def _orders_delta_fallback_log_level(reason: str) -> int:
        normalized = str(reason or "")
        expected_prefixes = (
            "empty_change_rows",
            "empty_delta_rows",
            "delta_no_effect",
            "unsupported_entities:",
        )
        if normalized in expected_prefixes or normalized.startswith(expected_prefixes):
            return logging.INFO
        return logging.WARNING

    def _store_patient_vitals(self, context: PatientSnapshotContext, snapshot) -> None:
        self._store_patient_vitals_by_key(context.cache_key(), snapshot, persist=True)

    def _store_patient_vitals_by_key(self, cache_key, snapshot, *, persist: bool) -> None:
        self._patient_vitals_cache[cache_key] = snapshot
        self._patient_vitals_cache.move_to_end(cache_key)
        self._track_patient_cache_key(self._patient_cache_index, cache_key)
        if persist and str(cache_key[0]) == "live" and str(cache_key[4]) == "live":
            persistent_snapshot_cache.store_snapshot(
                "patient_vitals",
                cache_key,
                dict(snapshot or {}),
                expires_at=persistent_snapshot_cache.expiry_from_cache_key(cache_key),
            )
        while len(self._patient_vitals_cache) > self.max_cached_patients:
            evicted_key, _ = self._patient_vitals_cache.popitem(last=False)
            self._drop_cache_validation(evicted_key)
            self._drop_cache_index_by_key(self._patient_cache_index, evicted_key)
            logger.info("[ReadCoordinator] evicted patient_vitals cache key=%s", evicted_key)

    def _store_patient_card(self, context: PatientSnapshotContext, snapshot) -> None:
        self._store_patient_card_by_key(context.cache_key(), snapshot, persist=True)

    def _store_patient_card_by_key(self, cache_key, snapshot, *, persist: bool) -> None:
        self._patient_card_cache[cache_key] = snapshot
        self._patient_card_cache.move_to_end(cache_key)
        self._track_patient_cache_key(self._patient_card_cache_index, cache_key)
        if persist and str(cache_key[0]) == "live" and str(cache_key[4]) == "live":
            persistent_snapshot_cache.store_snapshot(
                "patient_card",
                cache_key,
                dict(snapshot or {}),
                expires_at=persistent_snapshot_cache.expiry_from_cache_key(cache_key),
            )
        while len(self._patient_card_cache) > self.max_cached_patients:
            evicted_key, _ = self._patient_card_cache.popitem(last=False)
            self._drop_cache_validation(evicted_key)
            self._drop_cache_index_by_key(self._patient_card_cache_index, evicted_key)
            logger.info("[ReadCoordinator] evicted patient_card cache key=%s", evicted_key)

    def _store_patient_scope(self, context: PatientSnapshotContext, snapshot) -> None:
        self._store_patient_scope_by_key(context.cache_key(), snapshot, persist=True)

    def _store_patient_scope_by_key(self, cache_key, snapshot, *, persist: bool) -> None:
        self._patient_scope_cache[cache_key] = snapshot
        self._patient_scope_cache.move_to_end(cache_key)
        self._track_patient_cache_key(self._patient_scope_cache_index, cache_key)
        if persist and str(cache_key[0]) == "live" and str(cache_key[4]) == "live":
            persistent_snapshot_cache.store_snapshot(
                "patient_scope",
                cache_key,
                dict(snapshot or {}),
                expires_at=persistent_snapshot_cache.expiry_from_cache_key(cache_key),
            )
        while len(self._patient_scope_cache) > self.max_cached_patients * self.max_tabs_per_patient:
            evicted_key, _ = self._patient_scope_cache.popitem(last=False)
            self._drop_cache_validation(evicted_key)
            self._drop_cache_index_by_key(self._patient_scope_cache_index, evicted_key)
            logger.info("[ReadCoordinator] evicted patient_scope cache key=%s", evicted_key)

    def _store_orders_tab(self, context: OrdersContext, snapshot) -> None:
        cache_key = context.cache_key()
        self._orders_tab_cache[cache_key] = snapshot
        self._orders_tab_cache.move_to_end(cache_key)
        self._orders_stale_snapshots.pop(cache_key, None)
        self._track_patient_cache_entry(self._orders_cache_index, context)
        while len(self._orders_tab_cache) > self.max_cached_patients:
            evicted_key, _ = self._orders_tab_cache.popitem(last=False)
            self._drop_cache_index_by_key(self._orders_cache_index, evicted_key)
            self._orders_stale_versions.pop(evicted_key, None)
            self._orders_stale_snapshots.pop(evicted_key, None)
            logger.info(
                "[ReadCoordinator] evicted orders cache key=%s context_hash=%s",
                evicted_key,
                str(evicted_key[-1]),
            )

    @classmethod
    def _snapshot_content_hash(cls, payload: Dict[str, Any]) -> str:
        stable_payload = cls._stable_snapshot_value(payload)
        encoded = json.dumps(stable_payload, sort_keys=True, ensure_ascii=False, default=str)
        return sha1(encoded.encode("utf-8", errors="replace")).hexdigest()

    @classmethod
    def _stable_snapshot_value(cls, value):
        if isinstance(value, MappingProxyType):
            return cls._stable_snapshot_value(dict(value))
        if isinstance(value, dict):
            return {
                str(key): cls._stable_snapshot_value(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(value, (list, tuple, set)):
            return [cls._stable_snapshot_value(item) for item in value]
        if isinstance(value, datetime):
            return value.isoformat(timespec="microseconds")
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if hasattr(value, "__dict__"):
            return {
                "__class__": value.__class__.__name__,
                **{
                    str(key): cls._stable_snapshot_value(item)
                    for key, item in sorted(vars(value).items(), key=lambda pair: str(pair[0]))
                    if not str(key).startswith("_")
                },
            }
        return repr(value)

    def _normalize_snapshot_payload(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(snapshot or {})
        tuple_fields = (
            "vitals",
            "vitals_extended",
            "fluids",
            "chart_active_intervals",
            "orders",
            "patients",
            "events",
            "timeline",
            "active_intervals",
        )
        dict_fields = (
            "latest_values",
            "settings",
            "runtime_snapshot",
            "totals",
            "summary",
            "balance_runtime",
            "balance_calc",
        )
        for field in tuple_fields:
            if field in payload:
                payload[field] = tuple(payload.get(field) or ())
        for field in dict_fields:
            if payload.get(field):
                payload[field] = dict(payload.get(field) or {})
        if payload.get("effective_bounds"):
            payload["effective_bounds"] = tuple(payload.get("effective_bounds"))
        if "admin_rows" in payload:
            payload["admin_rows"] = tuple(dict(row) for row in (payload.get("admin_rows") or ()))
            payload["orders_admin_cursor"] = self._compute_admin_sync_cursor(payload.get("admin_rows") or ())
        return payload

    def _finalize_snapshot(
        self,
        *,
        snapshot: Dict[str, Any],
        scope: str,
        tab_name: str,
        cache_key,
        context_hash: str,
        role: str,
        mode: str,
        source_db: str,
        variant: str,
        load_strategy: str,
        load_trace_id: str,
        source: str,
        stale: bool,
        invalidate_reason: Optional[str],
    ):
        payload = self._normalize_snapshot_payload(snapshot)
        version = int(payload.get("change_id") or 0)
        content_hash = self._snapshot_content_hash(payload)
        payload["version"] = version
        payload["last_change_id"] = version
        payload["content_hash"] = content_hash
        payload["dedup_signature"] = (
            int(payload.get("admission_id") or 0),
            scope,
            version,
            content_hash,
        )
        payload["timestamp"] = datetime.now().isoformat(timespec="milliseconds")
        payload["scope"] = scope
        payload["tab_name"] = tab_name
        payload["role"] = role
        payload["mode"] = mode
        payload["source_db"] = source_db
        payload["variant"] = variant
        payload["cache_key"] = cache_key
        payload["context_hash"] = context_hash
        payload["load_strategy"] = load_strategy
        payload["load_trace_id"] = load_trace_id
        payload["source"] = source
        payload["stale"] = bool(stale)
        payload["invalidate_reason"] = invalidate_reason
        return MappingProxyType(payload)

    def _track_patient_cache_entry(
        self,
        index_store: dict[int, OrderedDict[str, None]],
        context: SnapshotContext,
    ) -> None:
        patient_tabs = index_store.setdefault(int(context.admission_id), OrderedDict())
        context_hash = context.hash()
        patient_tabs[context_hash] = None
        patient_tabs.move_to_end(context_hash)
        while len(patient_tabs) > self.max_tabs_per_patient:
            old_hash, _ = patient_tabs.popitem(last=False)
            self._evict_context_hash(index_store, int(context.admission_id), old_hash)

    def _track_patient_cache_key(self, index_store: dict[int, OrderedDict[str, None]], cache_key) -> None:
        patient_tabs = index_store.setdefault(int(cache_key[1]), OrderedDict())
        context_hash = str(cache_key[-1])
        patient_tabs[context_hash] = None
        patient_tabs.move_to_end(context_hash)
        while len(patient_tabs) > self.max_tabs_per_patient:
            old_hash, _ = patient_tabs.popitem(last=False)
            self._evict_context_hash(index_store, int(cache_key[1]), old_hash)

    def _evict_context_hash(
        self,
        index_store: dict[int, OrderedDict[str, None]],
        admission_id: int,
        context_hash: str,
    ) -> None:
        if index_store is self._patient_cache_index:
            target_cache = self._patient_vitals_cache
        elif index_store is self._patient_card_cache_index:
            target_cache = self._patient_card_cache
        elif index_store is self._patient_scope_cache_index:
            target_cache = self._patient_scope_cache
        else:
            target_cache = self._orders_tab_cache
        for cache_key in list(target_cache.keys()):
            if int(cache_key[1]) == int(admission_id) and str(cache_key[-1]) == str(context_hash):
                target_cache.pop(cache_key, None)
                self._drop_cache_validation(cache_key)
                self._orders_stale_versions.pop(cache_key, None)
                self._orders_stale_snapshots.pop(cache_key, None)
                logger.info(
                    "[ReadCoordinator] evicted cached tab admission_id=%s context_hash=%s",
                    admission_id,
                    context_hash,
                )

    def _drop_patient_cache_index(self, context: SnapshotContext) -> None:
        patient_tabs = self._patient_cache_index.get(int(context.admission_id))
        if not patient_tabs:
            return
        patient_tabs.pop(context.hash(), None)
        if not patient_tabs:
            self._patient_cache_index.pop(int(context.admission_id), None)

    def _drop_patient_card_cache_index(self, context: SnapshotContext) -> None:
        patient_tabs = self._patient_card_cache_index.get(int(context.admission_id))
        if not patient_tabs:
            return
        patient_tabs.pop(context.hash(), None)
        if not patient_tabs:
            self._patient_card_cache_index.pop(int(context.admission_id), None)

    def _drop_orders_cache_index(self, context: OrdersContext) -> None:
        patient_tabs = self._orders_cache_index.get(int(context.admission_id))
        if not patient_tabs:
            return
        patient_tabs.pop(context.hash(), None)
        if not patient_tabs:
            self._orders_cache_index.pop(int(context.admission_id), None)

    def _drop_cache_index_by_key(self, index_store: dict[int, OrderedDict[str, None]], cache_key) -> None:
        admission_id = int(cache_key[1])
        context_hash = str(cache_key[-1])
        patient_tabs = index_store.get(admission_id)
        if not patient_tabs:
            return
        patient_tabs.pop(context_hash, None)
        if not patient_tabs:
            index_store.pop(admission_id, None)

    @classmethod
    def _orders_snapshot_content_changed(cls, previous_snapshot, new_snapshot) -> bool:
        if previous_snapshot is None or new_snapshot is None:
            return False
        return cls._orders_snapshot_fingerprint(previous_snapshot) != cls._orders_snapshot_fingerprint(new_snapshot)

    @staticmethod
    def _orders_snapshot_fingerprint(snapshot) -> Tuple[Tuple[Any, ...], Tuple[Any, ...], bool, bool, bool]:
        orders = tuple(
            sorted(
                (
                    int(getattr(order, "id", 0) or 0),
                    int(getattr(order, "sort_order", 0) or 0),
                    str(getattr(order, "draft_sort_order", "") or ""),
                    str(getattr(order, "status", "") or ""),
                    int(getattr(order, "is_committed", 0) or 0),
                    str(getattr(order, "updated_at", "") or ""),
                    str(getattr(order, "latin", "") or ""),
                    str(getattr(order, "comment", "") or ""),
                )
                for order in (snapshot or {}).get("orders") or ()
                if order is not None
            )
        )
        admin_rows = tuple(
            sorted(
                (
                    int(dict(row).get("id") or 0),
                    int(dict(row).get("order_id") or 0),
                    str(dict(row).get("planned_time") or ""),
                    str(dict(row).get("status") or ""),
                    int(dict(row).get("is_committed", 0) or 0),
                    str(dict(row).get("updated_at") or ""),
                    str(dict(row).get("comment") or ""),
                )
                for row in (snapshot or {}).get("admin_rows") or ()
                if row is not None
            )
        )
        return (
            orders,
            admin_rows,
            bool((snapshot or {}).get("has_any_draft", False)),
            bool((snapshot or {}).get("has_any_administrations", False)),
            bool((snapshot or {}).get("has_any_orders", False)),
        )

    def _record_orders_load_telemetry(
        self,
        *,
        source: str,
        cache_hit: bool,
        delta_applied: bool,
        delta_failure_reason: str,
        delta_time_ms: float,
        elapsed_ms: float,
        fallback_after_delta: bool,
        admission_id: Optional[int] = None,
        context_hash: Optional[str] = None,
        trace_id: Optional[str] = None,
        priority: Optional[str] = None,
        fallback_used: Optional[int] = None,
        base: Optional[str] = None,
        version: Optional[int] = None,
    ) -> None:
        normalized_source = str(source or "refresh").strip().lower() or "refresh"
        metric_source = "cache" if cache_hit else ("click" if normalized_source == "user" else "refresh")
        record_metric(
            "orders_load_time_ms",
            round(float(elapsed_ms or 0.0), 3),
            admission_id=admission_id,
            source=metric_source,
            request_source=normalized_source,
            cache_hit=int(bool(cache_hit)),
            delta_applied=int(bool(delta_applied)),
            delta_failed=0 if str(delta_failure_reason or "0") in {"0", "", "none"} else 1,
            delta_time_ms=round(float(delta_time_ms or 0.0), 3),
            fallback_after_delta=int(bool(fallback_after_delta)),
            fallback_used=None if fallback_used is None else int(bool(fallback_used)),
            base=base,
            version=version,
            context_hash=context_hash,
            trace_id=trace_id,
            priority=self._normalize_priority(priority or "HIGH"),
        )
        now = time.monotonic()
        with self._orders_telemetry_lock:
            state = self._orders_telemetry
            if state.first_event_monotonic <= 0:
                state.first_event_monotonic = now
                state.first_event_wallclock_ts = datetime.now().isoformat(timespec="seconds")
            state.last_event_monotonic = now
            state.dirty_events += 1
            state.sample_count += 1
            state.total_loads += 1
            if normalized_source != "user":
                state.total_update_loads += 1
            if cache_hit:
                state.cache_hits += 1
            if normalized_source == "user":
                state.user_open_count += 1
                state.user_open_time_ms_total += max(0.0, float(elapsed_ms or 0.0))
            if normalized_source == "refresh":
                state.refresh_load_count += 1
            if delta_applied:
                state.delta_applied += 1
                state.delta_time_ms_total += max(0.0, float(delta_time_ms or 0.0))
                state.delta_time_samples += 1
            else:
                reason = str(delta_failure_reason or "0")
                if reason.startswith("stale_base_age_exceeded"):
                    state.delta_skipped_stale += 1
                elif reason.startswith("unsupported_entities:"):
                    state.delta_skip_unsupported_scope_count += 1
                elif reason not in {"0", "", "none"}:
                    state.delta_failed += 1
                    if any(
                        token in reason
                        for token in (
                            "delta_unknown_order",
                            "delta_no_effect",
                            "delta_without_visible_orders",
                            "empty_delta_rows",
                        )
                    ):
                        state.delta_fail_merge_error_count += 1
                    else:
                        state.delta_fail_other_count += 1
            if fallback_after_delta:
                state.fallback_after_delta += 1
            if not cache_hit:
                state.full_reload_count += 1
                state.full_reload_time_ms_total += max(0.0, float(elapsed_ms or 0.0))
            self._maybe_log_orders_telemetry_summary_locked()

    def _build_orders_telemetry_snapshot_locked(self) -> Dict[str, Any]:
        state = self._orders_telemetry
        total_update_loads = max(1, int(state.total_update_loads or 0))
        total_loads = max(1, int(state.total_loads or 0))
        delta_samples = max(1, int(state.delta_time_samples or 0))
        full_reload_count = max(1, int(state.full_reload_count or 0))
        user_open_count = max(1, int(state.user_open_count or 0))
        elapsed_sec = max(1e-6, float((state.last_event_monotonic or 0.0) - (state.first_event_monotonic or 0.0)))
        elapsed_minutes = max(elapsed_sec / 60.0, 1.0)
        return {
            "telemetry_window_start_ts": state.first_event_wallclock_ts or "",
            "telemetry_window_duration_sec": round(
                elapsed_sec if state.total_loads or state.sample_count else 0.0,
                3,
            ),
            "samples_count": int(state.sample_count),
            "total_loads": int(state.total_loads),
            "total_update_loads": int(state.total_update_loads),
            "delta_apply_rate": float(state.delta_applied) / float(total_update_loads),
            "delta_skip_rate": float(state.delta_skipped_stale) / float(total_update_loads),
            "delta_fail_rate": float(state.delta_failed) / float(total_update_loads),
            "avg_delta_time_ms": float(state.delta_time_ms_total) / float(delta_samples),
            "avg_full_reload_time_ms": float(state.full_reload_time_ms_total) / float(full_reload_count),
            "avg_orders_open_time_ms": float(state.user_open_time_ms_total) / float(user_open_count),
            "cache_hit_rate": float(state.cache_hits) / float(total_loads),
            "reloads_per_minute": float(state.refresh_load_count) / elapsed_minutes,
            "duplicate_load_prevented": int(state.duplicate_load_prevented),
            "race_reject_count": int(state.race_reject_count),
            "stale_apply_blocked": int(state.stale_apply_blocked),
            "fallback_after_delta": int(state.fallback_after_delta),
            "delta_applied": int(state.delta_applied),
            "delta_skipped_stale": int(state.delta_skipped_stale),
            "delta_skip_stale_base_count": int(state.delta_skipped_stale),
            "delta_skip_unsupported_scope_count": int(state.delta_skip_unsupported_scope_count),
            "delta_failed": int(state.delta_failed),
            "delta_fail_merge_error_count": int(state.delta_fail_merge_error_count),
            "delta_fail_other_count": int(state.delta_fail_other_count),
            "cache_hits": int(state.cache_hits),
            "refresh_load_count": int(state.refresh_load_count),
        }

    def _maybe_log_orders_telemetry_summary_locked(self) -> None:
        state = self._orders_telemetry
        if state.dirty_events <= 0:
            return
        now = time.monotonic()
        if state.first_event_monotonic <= 0:
            state.first_event_monotonic = now
        should_log = False
        if state.dirty_events >= READ_ORDERS_TELEMETRY_LOG_EVERY:
            should_log = True
        elif state.last_summary_monotonic <= 0:
            should_log = (now - state.first_event_monotonic) >= READ_ORDERS_TELEMETRY_LOG_INTERVAL_SEC
        elif (now - state.last_summary_monotonic) >= READ_ORDERS_TELEMETRY_LOG_INTERVAL_SEC:
            should_log = True
        if not should_log:
            return
        summary = self._build_orders_telemetry_snapshot_locked()
        logger.info(
            "[ReadCoordinator] ORDERS_TELEMETRY_SUMMARY window_start_ts=%s window_duration_sec=%.1f samples_count=%s total_loads=%s total_update_loads=%s delta_apply_rate=%.3f delta_skip_rate=%.3f delta_fail_rate=%.3f avg_delta_time_ms=%.2f avg_full_reload_time_ms=%.2f avg_orders_open_time_ms=%.2f cache_hit_rate=%.3f reloads_per_minute=%.2f duplicate_load_prevented=%s race_reject_count=%s stale_apply_blocked=%s fallback_after_delta=%s delta_skip_stale_base_count=%s delta_skip_unsupported_scope_count=%s delta_fail_merge_error_count=%s delta_fail_other_count=%s",
            summary["telemetry_window_start_ts"],
            summary["telemetry_window_duration_sec"],
            summary["samples_count"],
            summary["total_loads"],
            summary["total_update_loads"],
            summary["delta_apply_rate"],
            summary["delta_skip_rate"],
            summary["delta_fail_rate"],
            summary["avg_delta_time_ms"],
            summary["avg_full_reload_time_ms"],
            summary["avg_orders_open_time_ms"],
            summary["cache_hit_rate"],
            summary["reloads_per_minute"],
            summary["duplicate_load_prevented"],
            summary["race_reject_count"],
            summary["stale_apply_blocked"],
            summary["fallback_after_delta"],
            summary["delta_skip_stale_base_count"],
            summary["delta_skip_unsupported_scope_count"],
            summary["delta_fail_merge_error_count"],
            summary["delta_fail_other_count"],
        )
        state.last_summary_monotonic = now
        state.dirty_events = 0

    def _compute_admin_sync_cursor(self, rows) -> Dict[str, Any]:
        cursor = make_sync_cursor(EPOCH_SYNC_TS, 0)
        for row in rows or ():
            normalized = dict(row)
            row_updated_at = str(normalized.get("updated_at") or cursor["updated_at"])
            row_id = int(normalized.get("id") or 0)
            if is_cursor_newer(row_updated_at, row_id, cursor["updated_at"], cursor["id"]):
                cursor = make_sync_cursor(row_updated_at, row_id)
        return cursor

    @staticmethod
    def _snapshot_age_sec(snapshot) -> Optional[float]:
        raw_timestamp = str((snapshot or {}).get("timestamp") or "").strip()
        if not raw_timestamp:
            return None
        try:
            parsed = datetime.fromisoformat(raw_timestamp)
        except Exception:
            return None
        now = datetime.now(parsed.tzinfo) if parsed.tzinfo else datetime.now()
        return max(0.0, (now - parsed).total_seconds())

    def _next_trace_id(self, scope: str, context_hash: str) -> str:
        return f"{scope}-{next(self._trace_counter):06d}-{context_hash[:6]}"

    def _load_slot(
        self,
        *,
        priority: str,
        scope: str,
        context_hash: str,
        source: str,
        timeout_sec: float,
        trace_id: str,
    ):
        priority_name = self._normalize_priority(priority)
        priority_weight = _PRIORITY_WEIGHTS[priority_name]
        request_seq = next(self._load_counter)
        payload = (priority_weight, request_seq, trace_id, scope, context_hash, source)
        deadline = time.monotonic() + float(timeout_sec or READ_LOAD_TIMEOUT_SEC)

        class _LoadGuard:
            def __init__(self, coordinator):
                self.coordinator = coordinator

            def __enter__(self_inner):
                with self._load_condition:
                    heapq.heappush(self._load_waiting, payload)
                    while True:
                        head = self._load_waiting[0] if self._load_waiting else None
                        is_head = bool(head and head[1] == request_seq)
                        if is_head and self._active_loads < self.max_concurrent_loads:
                            heapq.heappop(self._load_waiting)
                            self._active_loads += 1
                            logger.info(
                                "[ReadCoordinator] load_start scope=%s priority=%s source=%s context_hash=%s trace_id=%s active=%s",
                                scope,
                                priority_name,
                                source,
                                context_hash,
                                trace_id,
                                self._active_loads,
                            )
                            return trace_id
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            self._remove_waiting_request(request_seq)
                            logger.error(
                                "[ReadCoordinator] load_timeout scope=%s priority=%s source=%s context_hash=%s trace_id=%s timeout_sec=%.1f",
                                scope,
                                priority_name,
                                source,
                                context_hash,
                                trace_id,
                                float(timeout_sec or READ_LOAD_TIMEOUT_SEC),
                            )
                            raise TimeoutError(
                                f"ReadCoordinator load timeout scope={scope} priority={priority_name} context_hash={context_hash}"
                            )
                        self._load_condition.wait(timeout=remaining)

            def __exit__(self_inner, exc_type, exc, tb):
                with self._load_condition:
                    self._active_loads = max(0, self._active_loads - 1)
                    logger.info(
                        "[ReadCoordinator] load_finish scope=%s priority=%s source=%s context_hash=%s trace_id=%s active=%s",
                        scope,
                        priority_name,
                        source,
                        context_hash,
                        trace_id,
                        self._active_loads,
                    )
                    self._load_condition.notify_all()

        return _LoadGuard(self)

    def _remove_waiting_request(self, request_seq: int) -> None:
        remaining = [entry for entry in self._load_waiting if entry[1] != request_seq]
        if len(remaining) != len(self._load_waiting):
            self._load_waiting = remaining
            heapq.heapify(self._load_waiting)

    @staticmethod
    def _normalize_priority(value: str) -> str:
        name = str(value or "MEDIUM").strip().upper()
        if name not in _PRIORITY_WEIGHTS:
            return "MEDIUM"
        return name
