import json
import re
import uuid
from datetime import datetime, timedelta
from typing import List, Mapping, Optional, Sequence

from rem_card.data.dao.sync_cursor import normalize_sync_cursor
from rem_card.services.order_domain_service import OrderDomainService
from rem_card.services.shift_service import ShiftService

from ..data.dto.remcard_dto import AdministrationDTO, OrderDTO, OrderType, OrderStatus
from ..data.dao.orders_dao import OrdersDAO


ORDER_CONFLICT_MESSAGE = "Данные изменены другим рабочим местом. Обновите карточку."
CVP_QUICK_ORDER_TEXT = "ЦВД (см.вод.ст.)"
CVP_QUICK_ORDER_KEY = "quick_cvp"


class OrderConflictError(RuntimeError):
    pass


class OrderService:
    def __init__(self, orders_dao: OrdersDAO):
        self.dao = orders_dao
        self._shifts = ShiftService()
        # Единый domain service на весь lifecycle OrderService:
        # сохраняет флаги "once" и не пересоздает maintenance-логику на каждом polling-read.
        self._domain_service = OrderDomainService(self.dao.db)

    def get_orders(self, admission_id: int, date=None, only_committed: bool = False) -> List[OrderDTO]:
        return self.dao.get_orders(admission_id, date, only_committed)

    def get_order_ids(self, admission_id: int, date=None, only_committed: bool = False) -> List[int]:
        return self.dao.get_order_ids(admission_id, date, only_committed)

    def _assign_next_sort_order_if_needed(self, dto: OrderDTO):
        if dto is None:
            return
        if getattr(dto, "sort_order", 0):
            return
        dto.sort_order = self.dao.get_next_sort_order(dto.admission_id, getattr(dto, "created_at", None))

    def _raise_order_conflict(self, order_id=None):
        raise OrderConflictError(ORDER_CONFLICT_MESSAGE)

    def _normalize_expected_revisions(self, expected_revisions) -> dict[int, int]:
        if not expected_revisions:
            return {}
        result: dict[int, int] = {}
        if isinstance(expected_revisions, Mapping):
            items = expected_revisions.items()
        else:
            items = expected_revisions
        for raw_order_id, raw_revision in items:
            if raw_order_id is None or raw_revision is None:
                continue
            try:
                result[int(raw_order_id)] = int(raw_revision)
            except Exception:
                continue
        return result

    def _assert_order_revisions(self, cursor, expected_revisions):
        expected = self._normalize_expected_revisions(expected_revisions)
        if not expected:
            return
        placeholders = ",".join("?" for _ in expected)
        cursor.execute(
            f"SELECT id, COALESCE(revision, 0) AS revision FROM orders WHERE id IN ({placeholders})",
            tuple(expected.keys()),
        )
        current = {int(row["id"]): int(row["revision"] or 0) for row in cursor.fetchall()}
        for order_id, revision in expected.items():
            if current.get(order_id) != revision:
                self._raise_order_conflict(order_id)

    def _assert_order_revision(self, cursor, order_id: int, expected_revision: Optional[int]):
        if expected_revision is None:
            return
        self._assert_order_revisions(cursor, {int(order_id): int(expected_revision)})

    @staticmethod
    def _draft_admin_shape(value) -> tuple | None:
        if value is None:
            return None
        if isinstance(value, Mapping):
            getter = value.get
        elif hasattr(value, "keys"):
            getter = lambda name, default=None: value[name] if name in value.keys() else default
        else:
            getter = lambda name, default=None: getattr(value, name, default)
        try:
            volume_ml = float(getter("volume_ml", 0.0) or 0.0)
        except Exception:
            volume_ml = 0.0
        return (
            str(getter("status", "") or ""),
            str(getter("cell_role", "") or ""),
            str(getter("big_chain_id", "") or ""),
            volume_ml,
        )

    @staticmethod
    def _draft_admin_token(value) -> tuple | None:
        if value is None:
            return None
        if isinstance(value, Mapping):
            getter = value.get
        elif hasattr(value, "keys"):
            getter = lambda name, default=None: value[name] if name in value.keys() else default
        else:
            getter = lambda name, default=None: getattr(value, name, default)
        actual_time = getter("actual_time")
        if isinstance(actual_time, datetime):
            actual_time = actual_time.isoformat()
        elif actual_time:
            actual_time = datetime.fromisoformat(str(actual_time).replace(" ", "T")).isoformat()
        return (
            None if getter("id") is None else int(getter("id")),
            int(getter("version", 0) or 0),
            *OrderService._draft_admin_shape(value),
            str(getter("comment", "") or ""),
            actual_time or None,
            None if getter("performer_id") is None else int(getter("performer_id")),
        )

    @staticmethod
    def _draft_admin_key(order_id, planned_time) -> tuple[int, str]:
        planned = planned_time
        if isinstance(planned, datetime):
            planned = planned.isoformat()
        else:
            planned = datetime.fromisoformat(str(planned).replace(" ", "T")).isoformat()
        return int(order_id), planned

    @staticmethod
    def _order_status_value(order: OrderDTO, *, pending_delete: bool) -> str:
        if pending_delete:
            return OrderStatus.DELETED.value
        raw = getattr(getattr(order, "status", None), "value", getattr(order, "status", None))
        if str(raw or "") in {OrderStatus.DELETED.value, OrderStatus.CANCELLED.value}:
            return str(raw)
        return OrderStatus.ACTIVE.value

    @staticmethod
    def _order_row_matches_effective(row, order: OrderDTO, *, sort_order: int, status: str) -> bool:
        if row is None:
            return False
        try:
            current_times = tuple(json.loads(row["specific_times"] or "[]"))
        except Exception:
            current_times = ()
        return (
            row["drug_key"] == order.drug_key
            and row["latin"] == order.latin
            and str(row["type"] or "") == str(getattr(order.type, "value", order.type) or "")
            and str(row["status"] or "") == status
            and float(row["dose_value"] or 0.0) == float(order.dose_value or 0.0)
            and str(row["dose_unit"] or "") == str(order.dose_unit or "")
            and int(row["is_per_kg"] or 0) == (1 if order.is_per_kg else 0)
            and int(row["frequency"] or 0) == int(order.frequency or 0)
            and current_times == tuple(order.specific_times or [])
            and (None if row["rate_ml_h"] is None else float(row["rate_ml_h"])) == OrderService._nullable_float(order.rate_ml_h)
            and (None if row["volume_total"] is None else float(row["volume_total"])) == OrderService._nullable_float(order.volume_total)
            and (None if row["duration_min"] is None else int(row["duration_min"])) == (
                None if order.duration_min is None else int(order.duration_min)
            )
            and int(row["sort_order"] or 0) == int(sort_order)
            and int(row["is_committed"] or 0) == 1
            and str(row["comment"] or "") == str(order.comment or "")
            and row["draft_sort_order"] is None
        )

    @staticmethod
    def _order_row_matches_clinical_fields(row, order: OrderDTO) -> bool:
        if row is None:
            return False
        try:
            current_times = tuple(json.loads(row["specific_times"] or "[]"))
        except Exception:
            current_times = ()
        return (
            row["drug_key"] == order.drug_key
            and row["latin"] == order.latin
            and str(row["type"] or "") == str(getattr(order.type, "value", order.type) or "")
            and float(row["dose_value"] or 0.0) == float(order.dose_value or 0.0)
            and str(row["dose_unit"] or "") == str(order.dose_unit or "")
            and int(row["is_per_kg"] or 0) == (1 if order.is_per_kg else 0)
            and int(row["frequency"] or 0) == int(order.frequency or 0)
            and current_times == tuple(order.specific_times or [])
            and (None if row["rate_ml_h"] is None else float(row["rate_ml_h"])) == OrderService._nullable_float(order.rate_ml_h)
            and (None if row["volume_total"] is None else float(row["volume_total"])) == OrderService._nullable_float(order.volume_total)
            and (None if row["duration_min"] is None else int(row["duration_min"])) == (
                None if order.duration_min is None else int(order.duration_min)
            )
            and str(row["comment"] or "") == str(order.comment or "")
        )

    @staticmethod
    def _fetch_latest_committed_admins(cursor, order_ids: Sequence[int], start: datetime, end: datetime) -> dict:
        normalized_ids = sorted({int(value) for value in order_ids if value is not None and int(value) > 0})
        if not normalized_ids:
            return {}
        placeholders = ",".join("?" for _ in normalized_ids)
        cursor.execute(
            f"""
            SELECT a.*
            FROM administrations a
            WHERE a.order_id IN ({placeholders})
              AND a.planned_time >= ? AND a.planned_time < ?
              AND a.is_committed = 1
              AND a.id = (
                  SELECT MAX(a2.id)
                  FROM administrations a2
                  WHERE a2.order_id = a.order_id
                    AND a2.planned_time = a.planned_time
                    AND a2.is_committed = 1
              )
            """,
            (*normalized_ids, start.isoformat(), end.isoformat()),
        )
        result = {}
        for row in cursor.fetchall():
            planned_key = datetime.fromisoformat(str(row["planned_time"]).replace(" ", "T")).isoformat()
            result[(int(row["order_id"]), planned_key)] = row
        return result

    def _assert_active_order_ids_match(
        self,
        cursor,
        admission_id: int,
        start: datetime,
        end: datetime,
        expected_active_order_ids: Optional[Sequence[int]],
    ) -> None:
        if expected_active_order_ids is None:
            return
        expected_ids = {
            int(order_id)
            for order_id in expected_active_order_ids
            if order_id is not None and int(order_id) > 0
        }
        cursor.execute(
            """
            SELECT id
            FROM orders
            WHERE admission_id = ?
              AND datetime >= ? AND datetime < ?
              AND is_committed = 1
              AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
            """,
            (int(admission_id), start.isoformat(), end.isoformat()),
        )
        current_ids = {int(row["id"]) for row in cursor.fetchall()}
        if current_ids != expected_ids:
            self._raise_order_conflict()

    @staticmethod
    def _load_local_draft_orders(cursor, orders: Sequence[OrderDTO]) -> tuple[list[OrderDTO], list[int], dict]:
        effective_orders = [order for order in (orders or []) if order is not None]
        existing_ids = sorted(
            {
                int(order.id)
                for order in effective_orders
                if getattr(order, "id", None) is not None and int(order.id) > 0
            }
        )
        current_orders = {}
        if existing_ids:
            placeholders = ",".join("?" for _ in existing_ids)
            cursor.execute(
                f"SELECT * FROM orders WHERE id IN ({placeholders})",
                tuple(existing_ids),
            )
            current_orders = {int(row["id"]): row for row in cursor.fetchall()}
        return effective_orders, existing_ids, current_orders

    @staticmethod
    def _insert_local_draft_order(
        cursor,
        admission_id: int,
        start: datetime,
        order: OrderDTO,
        desired_sort_order: int,
    ) -> int:
        created_at = getattr(order, "created_at", None) or start
        cursor.execute(
            """
            INSERT INTO orders (
                admission_id, datetime, text, drug_key, latin, type, status,
                dose_value, dose_unit, is_per_kg, frequency, specific_times,
                rate_ml_h, volume_total, duration_min, sort_order, draft_sort_order,
                is_committed, created_at, comment, last_modified_by, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, ?, ?, 'doctor',
                      STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
            """,
            (
                int(admission_id),
                created_at.isoformat(),
                f"{order.latin} {float(order.dose_value or 0):g} {order.dose_unit or ''}".strip(),
                order.drug_key,
                order.latin,
                getattr(order.type, "value", order.type),
                OrderStatus.ACTIVE.value,
                float(order.dose_value or 0.0),
                order.dose_unit,
                1 if order.is_per_kg else 0,
                int(order.frequency or 0),
                json.dumps(order.specific_times or []),
                order.rate_ml_h,
                order.volume_total,
                order.duration_min,
                desired_sort_order,
                created_at.isoformat(),
                order.comment or "",
            ),
        )
        return int(cursor.lastrowid)

    def _assert_existing_local_draft_order(
        self,
        current,
        local_order_id: int,
        admission_id: int,
        start: datetime,
        end: datetime,
    ) -> None:
        if current is None or int(current["admission_id"] or 0) != int(admission_id):
            self._raise_order_conflict(local_order_id)
        current_dt = datetime.fromisoformat(str(current["datetime"]).replace(" ", "T"))
        if not (start <= current_dt < end):
            self._raise_order_conflict(local_order_id)

    def _assert_no_committed_nurse_mark(self, cursor, order_id: int) -> None:
        cursor.execute(
            """
            SELECT 1
            FROM administrations current_admin
            WHERE current_admin.order_id = ?
              AND current_admin.is_committed = 1
              AND current_admin.comment IN ('nurse_executed', 'nurse_not_executed')
              AND current_admin.id = (
                  SELECT MAX(latest_admin.id)
                  FROM administrations latest_admin
                  WHERE latest_admin.order_id = current_admin.order_id
                    AND latest_admin.planned_time = current_admin.planned_time
                    AND latest_admin.is_committed = 1
              )
            LIMIT 1
            """,
            (order_id,),
        )
        if cursor.fetchone() is not None:
            self._raise_order_conflict(order_id)

    @staticmethod
    def _update_local_draft_order(
        cursor,
        order: OrderDTO,
        local_order_id: int,
        desired_sort_order: int,
        status: str,
    ) -> None:
        cursor.execute(
            """
            UPDATE orders
            SET text = ?, drug_key = ?, latin = ?, type = ?, status = ?,
                dose_value = ?, dose_unit = ?, is_per_kg = ?, frequency = ?,
                specific_times = ?, rate_ml_h = ?, volume_total = ?, duration_min = ?,
                sort_order = ?, draft_sort_order = NULL, is_committed = 1,
                comment = ?, last_modified_by = 'doctor',
                revision = COALESCE(revision, 0) + 1,
                updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
            WHERE id = ?
            """,
            (
                f"{order.latin} {float(order.dose_value or 0):g} {order.dose_unit or ''}".strip(),
                order.drug_key,
                order.latin,
                getattr(order.type, "value", order.type),
                status,
                float(order.dose_value or 0.0),
                order.dose_unit,
                1 if order.is_per_kg else 0,
                int(order.frequency or 0),
                json.dumps(order.specific_times or []),
                order.rate_ml_h,
                order.volume_total,
                order.duration_min,
                desired_sort_order,
                order.comment or "",
                local_order_id,
            ),
        )

    def _commit_local_draft_order(
        self,
        cursor,
        admission_id: int,
        start: datetime,
        end: datetime,
        order: OrderDTO,
        position: int,
        current_orders: Mapping[int, object],
    ) -> tuple[int, int] | None:
        raw_order_id = getattr(order, "id", None)
        if raw_order_id is None:
            raise ValueError("Local draft order has no temporary identifier")
        local_order_id = int(raw_order_id)
        desired_sort_order = int(getattr(order, "sort_order", position) or 0)
        pending_delete = bool(getattr(order, "_pending_delete", False))
        status = self._order_status_value(order, pending_delete=pending_delete)

        if local_order_id <= 0:
            if pending_delete or status in {OrderStatus.DELETED.value, OrderStatus.CANCELLED.value}:
                return None
            real_order_id = self._insert_local_draft_order(
                cursor,
                admission_id,
                start,
                order,
                desired_sort_order,
            )
            return local_order_id, real_order_id

        current = current_orders.get(local_order_id)
        self._assert_existing_local_draft_order(current, local_order_id, admission_id, start, end)
        clinical_fields_changed = not self._order_row_matches_clinical_fields(current, order)
        if pending_delete or clinical_fields_changed:
            self._assert_no_committed_nurse_mark(cursor, local_order_id)
        if not self._order_row_matches_effective(
            current,
            order,
            sort_order=desired_sort_order,
            status=status,
        ):
            self._update_local_draft_order(
                cursor,
                order,
                local_order_id,
                desired_sort_order,
                status,
            )
        return local_order_id, local_order_id

    def _commit_local_draft_orders(
        self,
        cursor,
        admission_id: int,
        start: datetime,
        end: datetime,
        effective_orders: Sequence[OrderDTO],
        current_orders: Mapping[int, object],
    ) -> dict[int, int]:
        order_id_map: dict[int, int] = {}
        for position, order in enumerate(effective_orders):
            saved_ids = self._commit_local_draft_order(
                cursor,
                admission_id,
                start,
                end,
                order,
                position,
                current_orders,
            )
            if saved_ids is not None:
                local_order_id, real_order_id = saved_ids
                order_id_map[local_order_id] = real_order_id
        return order_id_map

    def _normalize_local_draft_admins(
        self,
        admin_map: Mapping[tuple, AdministrationDTO],
        dirty_admin_keys: Sequence[tuple],
        baseline_admin_map: Mapping[tuple, AdministrationDTO],
    ) -> tuple[dict, dict, set]:
        normalized_baseline = {
            self._draft_admin_key(key[0], key[1]): value
            for key, value in (baseline_admin_map or {}).items()
            if key and len(key) >= 2 and int(key[0]) > 0
        }
        normalized_desired = {
            self._draft_admin_key(key[0], key[1]): value
            for key, value in (admin_map or {}).items()
            if key and len(key) >= 2
        }
        normalized_dirty = {
            self._draft_admin_key(key[0], key[1])
            for key in (dirty_admin_keys or [])
            if key and len(key) >= 2
        }
        return normalized_baseline, normalized_desired, normalized_dirty

    @staticmethod
    def _collect_temporary_admin_chains(
        normalized_dirty: set,
        normalized_desired: Mapping[tuple, AdministrationDTO],
        start: datetime,
        end: datetime,
    ) -> dict[tuple[int, str], list[tuple[datetime, str]]]:
        temporary_chain_groups: dict[tuple[int, str], list[tuple[datetime, str]]] = {}
        for local_order_id, planned_key in normalized_dirty:
            desired = normalized_desired.get((local_order_id, planned_key))
            if desired is None:
                continue
            desired_status = str(getattr(desired, "status", "") or "")
            if desired_status in {"deleted", "cancelled"}:
                continue
            planned_dt = datetime.fromisoformat(str(planned_key).replace(" ", "T"))
            if not (start <= planned_dt < end):
                raise ValueError("Draft administration is outside the selected shift")
            chain_value = str(getattr(desired, "big_chain_id", "") or "")
            if chain_value.startswith(("optimistic:", "local-copy:")):
                temporary_chain_groups.setdefault((local_order_id, chain_value), []).append(
                    (planned_dt, str(getattr(desired, "cell_role", "single") or "single"))
                )
        return temporary_chain_groups

    @staticmethod
    def _validate_temporary_admin_chains(
        temporary_chain_groups: Mapping[tuple[int, str], list[tuple[datetime, str]]],
    ) -> None:
        for chain_items in temporary_chain_groups.values():
            chain_items.sort(key=lambda item: item[0])
            roles = [item[1] for item in chain_items]
            if len(chain_items) == 1:
                if roles != ["single"]:
                    raise ValueError("Invalid single-cell administration chain")
                continue
            if roles[0] != "start" or roles[-1] != "end" or any(
                role != "body" for role in roles[1:-1]
            ):
                raise ValueError("Invalid administration chain roles")
            if any(
                current_time - previous_time != timedelta(hours=1)
                for (previous_time, _), (current_time, _) in zip(chain_items, chain_items[1:])
            ):
                raise ValueError("Administration chain must be continuous")

    @staticmethod
    def _resolve_draft_admin_values(desired, current) -> tuple[str, str, object, float]:
        desired_status = str(getattr(desired, "status", "deleted") or "deleted") if desired else "deleted"
        desired_role = str(getattr(desired, "cell_role", "single") or "single") if desired else str(
            (current["cell_role"] if current is not None else "single") or "single"
        )
        desired_chain = getattr(desired, "big_chain_id", None) if desired else (
            current["big_chain_id"] if current is not None else None
        )
        desired_volume = float(getattr(desired, "volume_ml", 0.0) or 0.0) if desired else float(
            (current["volume_ml"] if current is not None else 0.0) or 0.0
        )
        return desired_status, desired_role, desired_chain, desired_volume

    @staticmethod
    def _insert_local_draft_admin(
        cursor,
        real_order_id: int,
        planned_key: str,
        desired,
        current,
        desired_status: str,
        desired_role: str,
        desired_chain,
        desired_volume: float,
        current_mark: str,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO administrations (
                order_id, chain_id, big_chain_id, cell_role, planned_time,
                actual_time, performer_id, status, version, comment,
                dose_given, volume_ml, is_committed, last_modified_by, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'doctor',
                      STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
            """,
            (
                real_order_id,
                getattr(desired, "chain_id", None) if desired else (
                    current["chain_id"] if current is not None else None
                ),
                desired_chain,
                desired_role,
                planned_key,
                current["actual_time"] if current is not None else None,
                current["performer_id"] if current is not None else None,
                desired_status,
                int((current["version"] if current is not None else -1) or 0) + 1,
                current_mark,
                current["dose_given"] if current is not None else None,
                desired_volume,
            ),
        )

    def _commit_local_draft_admin(
        self,
        cursor,
        local_order_id: int,
        real_order_id: int,
        planned_key: str,
        desired,
        current,
        normalized_baseline: Mapping[tuple, AdministrationDTO],
        temporary_chain_map: Mapping[tuple[int, str], str],
    ) -> None:
        if local_order_id > 0:
            baseline = normalized_baseline.get((local_order_id, planned_key))
            if self._draft_admin_token(current) != self._draft_admin_token(baseline):
                self._raise_order_conflict(local_order_id)

        desired_status, desired_role, desired_chain, desired_volume = self._resolve_draft_admin_values(
            desired,
            current,
        )
        temporary_chain_key = (local_order_id, str(desired_chain or ""))
        if temporary_chain_key in temporary_chain_map:
            desired_chain = temporary_chain_map[temporary_chain_key]

        current_mark = str((current["comment"] if current is not None else "") or "")
        if desired_status in {"deleted", "cancelled"} and current_mark in {
            "nurse_executed", "nurse_not_executed"
        }:
            self._raise_order_conflict(local_order_id)

        target_shape = (desired_status, desired_role, str(desired_chain or ""), desired_volume)
        if self._draft_admin_shape(current) == target_shape:
            return
        self._insert_local_draft_admin(
            cursor,
            real_order_id,
            planned_key,
            desired,
            current,
            desired_status,
            desired_role,
            desired_chain,
            desired_volume,
            current_mark,
        )

    def _commit_local_draft_admins(
        self,
        cursor,
        order_id_map: Mapping[int, int],
        current_admins: Mapping[tuple, object],
        normalized_baseline: Mapping[tuple, AdministrationDTO],
        normalized_desired: Mapping[tuple, AdministrationDTO],
        normalized_dirty: set,
        temporary_chain_map: Mapping[tuple[int, str], str],
    ) -> None:
        for local_order_id, planned_key in sorted(normalized_dirty, key=lambda item: (item[0], item[1])):
            real_order_id = order_id_map.get(local_order_id)
            if real_order_id is None:
                continue
            desired = normalized_desired.get((local_order_id, planned_key))
            current = current_admins.get((real_order_id, planned_key))
            self._commit_local_draft_admin(
                cursor,
                local_order_id,
                real_order_id,
                planned_key,
                desired,
                current,
                normalized_baseline,
                temporary_chain_map,
            )

    def commit_local_draft(
        self,
        admission_id: int,
        shift_date: datetime,
        *,
        orders: Sequence[OrderDTO],
        admin_map: Mapping[tuple, AdministrationDTO],
        dirty_admin_keys: Sequence[tuple],
        baseline_admin_map: Mapping[tuple, AdministrationDTO],
        expected_revisions=None,
        expected_active_order_ids: Optional[Sequence[int]] = None,
    ) -> dict[int, int]:
        """Apply the doctor's in-memory draft as one central transaction.

        Intermediate clicks never reach SQLite.  Only the final effective order
        list and the final shape of changed cells are persisted here.
        """
        with self.dao.db.remcard_transaction(source="orders_commit_local_draft") as cursor:
            start, end = self._resolve_shift_bounds(cursor, admission_id, shift_date)
            self._assert_order_revisions(cursor, expected_revisions)
            self._assert_active_order_ids_match(
                cursor,
                admission_id,
                start,
                end,
                expected_active_order_ids,
            )
            effective_orders, existing_ids, current_orders = self._load_local_draft_orders(cursor, orders)
            order_id_map = self._commit_local_draft_orders(
                cursor,
                admission_id,
                start,
                end,
                effective_orders,
                current_orders,
            )

            current_admins = self._fetch_latest_committed_admins(cursor, existing_ids, start, end)
            normalized_baseline, normalized_desired, normalized_dirty = self._normalize_local_draft_admins(
                admin_map,
                dirty_admin_keys,
                baseline_admin_map,
            )
            temporary_chain_groups = self._collect_temporary_admin_chains(
                normalized_dirty,
                normalized_desired,
                start,
                end,
            )
            self._validate_temporary_admin_chains(temporary_chain_groups)
            temporary_chain_map = {
                group_key: str(uuid.uuid4())
                for group_key in temporary_chain_groups
            }
            self._commit_local_draft_admins(
                cursor,
                order_id_map,
                current_admins,
                normalized_baseline,
                normalized_desired,
                normalized_dirty,
                temporary_chain_map,
            )

            self._domain_service.sync_transfusions_for_admission(cursor, int(admission_id))
            return order_id_map

    def add_order(self, dto: OrderDTO):
        with self.dao.db.remcard_transaction():
            self._assign_next_sort_order_if_needed(dto)
            return self.dao.add_order(dto)

    @staticmethod
    def _nullable_float(value) -> Optional[float]:
        return None if value is None else float(value)

    @staticmethod
    def _order_type_value(value) -> str:
        return getattr(value, "value", value)

    def _order_edit_values(self, row, dto: OrderDTO) -> tuple[tuple[object, ...], tuple[object, ...]]:
        current_times = json.loads(row["specific_times"] or "[]")
        proposed_times = list(dto.specific_times or [])
        current = (
            row["drug_key"],
            row["latin"],
            row["type"],
            row["status"],
            float(row["dose_value"] or 0),
            row["dose_unit"],
            1 if row["is_per_kg"] else 0,
            int(row["frequency"] or 0),
            tuple(current_times),
            self._nullable_float(row["rate_ml_h"]),
            self._nullable_float(row["volume_total"]),
            None if row["duration_min"] is None else int(row["duration_min"]),
            row["comment"] or "",
        )
        proposed = (
            dto.drug_key,
            dto.latin,
            self._order_type_value(dto.type),
            "active",
            float(dto.dose_value or 0),
            dto.dose_unit,
            1 if dto.is_per_kg else 0,
            int(dto.frequency or 0),
            tuple(proposed_times),
            self._nullable_float(dto.rate_ml_h),
            self._nullable_float(dto.volume_total),
            None if dto.duration_min is None else int(dto.duration_min),
            dto.comment or "",
        )
        return current, proposed

    def update_order(self, order_id: int, dto: OrderDTO, expected_revision: Optional[int] = None) -> OrderDTO:
        with self.dao.db.remcard_transaction() as cursor:
            self._assert_order_revision(cursor, order_id, expected_revision)
            cursor.execute(
                """
                SELECT *
                FROM orders
                WHERE id = ?
                """,
                (int(order_id),),
            )
            current = cursor.fetchone()
            if current is None:
                self._raise_order_conflict(order_id)

            text_rep = f"{dto.latin} {float(dto.dose_value or 0):g} {dto.dose_unit or ''}".strip()
            admission_id = int(current["admission_id"])
            current_values, proposed_values = self._order_edit_values(current, dto)
            if current_values == proposed_values:
                result = self.dao._map_order_row(current)
                result.admission_id = admission_id
                return result
            next_is_committed = 0
            cursor.execute(
                """
                UPDATE orders
                SET text = ?,
                    drug_key = ?,
                    latin = ?,
                    type = ?,
                    status = 'active',
                    dose_value = ?,
                    dose_unit = ?,
                    is_per_kg = ?,
                    frequency = ?,
                    specific_times = ?,
                    rate_ml_h = ?,
                    volume_total = ?,
                    duration_min = ?,
                    is_committed = ?,
                    comment = ?,
                    last_modified_by = 'doctor',
                    revision = COALESCE(revision, 0) + 1,
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                """,
                (
                    text_rep,
                    dto.drug_key,
                    dto.latin,
                    dto.type.value,
                    float(dto.dose_value or 0),
                    dto.dose_unit,
                    1 if dto.is_per_kg else 0,
                    int(dto.frequency or 0),
                    json.dumps(dto.specific_times or []),
                    dto.rate_ml_h,
                    dto.volume_total,
                    dto.duration_min,
                    next_is_committed,
                    dto.comment,
                    int(order_id),
                ),
            )
            cursor.execute("SELECT * FROM orders WHERE id = ?", (int(order_id),))
            updated = cursor.fetchone()
            result = self.dao._map_order_row(updated)
            result.admission_id = admission_id
            return result

    def add_orders_batch(self, orders: List[OrderDTO]):
        with self.dao.db.remcard_transaction():
            next_sort_order_by_context: dict[tuple[int, str], int] = {}
            for dto in orders:
                if not getattr(dto, "sort_order", 0):
                    sort_date = getattr(dto, "created_at", None)
                    shift_key = ""
                    if isinstance(sort_date, datetime):
                        shift_key = self._shifts.get_day_period(sort_date)[0].isoformat()
                    context_key = (
                        int(dto.admission_id),
                        shift_key,
                    )
                    if context_key not in next_sort_order_by_context:
                        next_sort_order_by_context[context_key] = self.dao.get_next_sort_order(
                            dto.admission_id,
                            sort_date,
                        )
                    dto.sort_order = next_sort_order_by_context[context_key]
                    next_sort_order_by_context[context_key] += 1
                self.dao.add_order(dto)

    @staticmethod
    def _normalize_cvp_order_text(value: object) -> str:
        text = str(value or "").strip().lower().replace("ё", "е")
        return re.sub(r"[^0-9a-zа-я]+", "", text)

    @classmethod
    def _is_cvp_order_text(cls, value: object) -> bool:
        normalized = cls._normalize_cvp_order_text(value)
        target = cls._normalize_cvp_order_text(CVP_QUICK_ORDER_TEXT)
        return normalized == target or normalized == f"{target}0"

    @classmethod
    def _row_is_cvp_order(cls, row) -> bool:
        if row is None:
            return False
        return cls._is_cvp_order_text(row["latin"]) or cls._is_cvp_order_text(row["text"])

    def has_cvp_order(self, admission_id: int, shift_date: datetime) -> bool:
        start, end = self._shifts.get_day_period(shift_date)
        rows = self.dao.db.fetch_all_remcard(
            """
            SELECT latin, text
            FROM orders
            WHERE admission_id = ?
              AND datetime >= ? AND datetime < ?
              AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
            """,
            (int(admission_id), start.isoformat(), end.isoformat()),
        )
        return any(self._row_is_cvp_order(row) for row in rows)

    def add_cvp_order_if_missing(self, admission_id: int, shift_date: datetime) -> tuple[Optional[OrderDTO], bool]:
        start, end = self._shifts.get_day_period(shift_date)
        now = datetime.now()
        created_at = now if start <= now < end else start

        with self.dao.db.remcard_transaction(source="orders_add_cvp_if_missing") as cursor:
            cursor.execute(
                """
                SELECT *
                FROM orders
                WHERE admission_id = ?
                  AND datetime >= ? AND datetime < ?
                  AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                ORDER BY COALESCE(draft_sort_order, sort_order, 0) ASC, created_at ASC, id ASC
                """,
                (int(admission_id), start.isoformat(), end.isoformat()),
            )
            for row in cursor.fetchall():
                if self._row_is_cvp_order(row):
                    return self.dao._map_order_row(row), False

            sort_order = self.dao.get_next_sort_order(int(admission_id), created_at)
            cursor.execute(
                """
                INSERT INTO orders (
                    admission_id, datetime, text, drug_key, latin, type, status,
                    dose_value, dose_unit, is_per_kg, frequency, specific_times,
                    rate_ml_h, volume_total, duration_min, sort_order, is_committed,
                    created_at, comment, last_modified_by, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
                """,
                (
                    int(admission_id),
                    created_at.isoformat(),
                    CVP_QUICK_ORDER_TEXT,
                    CVP_QUICK_ORDER_KEY,
                    CVP_QUICK_ORDER_TEXT,
                    OrderType.MEDICATION.value,
                    OrderStatus.ACTIVE.value,
                    0.0,
                    "",
                    0,
                    1,
                    "[]",
                    None,
                    None,
                    0,
                    sort_order,
                    0,
                    created_at.isoformat(),
                    "",
                    "doctor",
                ),
            )
            order_id = cursor.lastrowid
            cursor.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
            row = cursor.fetchone()
            return self.dao._map_order_row(row) if row else None, True

    def update_order_status(self, order_id: int, status: str, expected_revision: Optional[int] = None):
        if expected_revision is None:
            with self.dao.db.remcard_transaction():
                self.dao.update_status(order_id, status)
            return
        with self.dao.db.remcard_transaction() as cursor:
            self._assert_order_revision(cursor, order_id, expected_revision)
            cursor.execute(
                """
                UPDATE orders
                SET status = ?,
                    last_modified_by = 'doctor',
                    revision = COALESCE(revision, 0) + 1,
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                """,
                (status, order_id),
            )

    def has_drafts(self, admission_id: int, shift_date: Optional[datetime] = None) -> bool:
        if shift_date is None:
            query = """
                SELECT EXISTS (
                    SELECT 1 FROM orders WHERE admission_id = ? AND is_committed = 0
                    UNION ALL
                    SELECT 1 FROM orders WHERE admission_id = ? AND draft_sort_order IS NOT NULL
                    UNION ALL
                    SELECT 1 FROM administrations a
                    JOIN orders o ON a.order_id = o.id
                    WHERE o.admission_id = ? AND a.is_committed = 0
                )
            """
            res = self.dao.db.fetch_one_remcard(query, (admission_id, admission_id, admission_id))
            return bool(res[0]) if res else False

        start, end = self._shifts.get_day_period(shift_date)
        query = """
            SELECT EXISTS (
                SELECT 1
                FROM orders
                WHERE admission_id = ?
                  AND datetime >= ? AND datetime < ?
                  AND (is_committed = 0 OR draft_sort_order IS NOT NULL)
                UNION ALL
                SELECT 1
                FROM administrations a
                JOIN orders o ON a.order_id = o.id
                WHERE o.admission_id = ?
                  AND a.planned_time >= ? AND a.planned_time < ?
                  AND a.is_committed = 0
            )
        """
        res = self.dao.db.fetch_one_remcard(
            query,
            (
                admission_id,
                start.isoformat(),
                end.isoformat(),
                admission_id,
                start.isoformat(),
                end.isoformat(),
            ),
        )
        return bool(res[0]) if res else False

    def has_administrations(self, admission_id: int, shift_date: datetime, only_committed: bool = False) -> bool:
        start, end = self._shifts.get_day_period(shift_date)
        query = """
            SELECT EXISTS (
                SELECT 1 FROM administrations a
                JOIN orders o ON a.order_id = o.id
                WHERE o.admission_id = ?
                AND a.planned_time >= ? AND a.planned_time < ?
                AND COALESCE(a.status, '') != 'deleted'
            )
        """
        if only_committed:
            query = query.replace(
                "AND COALESCE(a.status, '') != 'deleted'",
                "AND a.is_committed = 1 AND COALESCE(a.status, '') != 'deleted'",
            )
        res = self.dao.db.fetch_one_remcard(query, (admission_id, start.isoformat(), end.isoformat()))
        return bool(res[0]) if res else False

    def get_latest_admin_rows(
        self,
        admission_id: int,
        shift_date: datetime,
        *,
        only_committed: bool = False,
        include_deleted: bool = False,
        include_cancelled: bool = False,
        include_deleted_orders: bool = True,
        updated_after=None,
        cancel_check=None,
    ):
        start, end = self._shifts.get_day_period(shift_date)
        return self.get_latest_admin_rows_for_order_ids(
            order_ids=None,
            start_dt=start,
            end_dt=end,
            admission_id=admission_id,
            only_committed=only_committed,
            include_deleted=include_deleted,
            include_cancelled=include_cancelled,
            include_deleted_orders=include_deleted_orders,
            updated_after=updated_after,
            cancel_check=cancel_check,
        )

    def get_latest_admin_rows_for_order_ids(
        self,
        *,
        start_dt: datetime,
        end_dt: datetime,
        order_ids: Optional[Sequence[int]] = None,
        admission_id: Optional[int] = None,
        only_committed: bool = False,
        include_deleted: bool = False,
        include_cancelled: bool = False,
        include_deleted_orders: bool = True,
        updated_after=None,
        cancel_check=None,
    ):
        if order_ids is not None and not order_ids:
            return []

        if order_ids is None and admission_id is None:
            return []

        params: List[object] = []
        join_filter_main = ""
        join_filter_sub = ""

        if order_ids is not None:
            placeholders = ",".join("?" for _ in order_ids)
            join_filter_main = f" AND a.order_id IN ({placeholders})"
            join_filter_sub = f" AND a2.order_id IN ({placeholders})"
            params.extend(order_ids)
        else:
            join_filter_main = " AND o.admission_id = ?"
            join_filter_sub = " AND o2.admission_id = ?"
            params.append(admission_id)

        query = f"""
            SELECT a.*
            FROM administrations a
            JOIN orders o ON a.order_id = o.id
            WHERE a.planned_time >= ? AND a.planned_time < ?
            {join_filter_main}
            AND a.id IN (
                SELECT MAX(a2.id)
                FROM administrations a2
                JOIN orders o2 ON a2.order_id = o2.id
                WHERE a2.planned_time >= ? AND a2.planned_time < ?
                {join_filter_sub}
        """

        if only_committed:
            query += " AND a2.is_committed = 1"
        query += " GROUP BY a2.order_id, a2.planned_time )"

        if only_committed:
            query += " AND a.is_committed = 1"
        if not include_deleted:
            query += " AND COALESCE(a.status, '') != 'deleted'"
        if not include_cancelled:
            query += " AND COALESCE(a.status, '') != 'cancelled'"
        if not include_deleted_orders:
            query += " AND COALESCE(o.status, '') != 'deleted'"
        updated_after_ts = None
        updated_after_id = 0
        if updated_after is not None:
            updated_after_ts, updated_after_id = normalize_sync_cursor(updated_after)
            query += """
            AND (
                COALESCE(STRFTIME('%Y-%m-%d %H:%M:%f', a.updated_at), '') > ?
                OR (
                    COALESCE(STRFTIME('%Y-%m-%d %H:%M:%f', a.updated_at), '') = ?
                    AND a.id > ?
                )
            )
            """

        if updated_after is not None:
            query += " ORDER BY COALESCE(STRFTIME('%Y-%m-%d %H:%M:%f', a.updated_at), '') ASC, a.id ASC"
        else:
            query += " ORDER BY a.planned_time ASC, a.id ASC"

        final_params: List[object] = [start_dt.isoformat(), end_dt.isoformat(), *params, start_dt.isoformat(), end_dt.isoformat(), *params]
        if updated_after is not None:
            final_params.extend([updated_after_ts, updated_after_ts, int(updated_after_id)])
        return self.dao.db.fetch_all_remcard(query, tuple(final_params), cancel_check=cancel_check)

    def _apply_order_sort_order(
        self,
        cursor,
        *,
        admission_id: int,
        shift_date: Optional[datetime],
        ordered_order_ids: Optional[Sequence[int]],
        target_column: str = "sort_order",
    ):
        if not ordered_order_ids or shift_date is None:
            return
        if target_column not in {"sort_order", "draft_sort_order"}:
            raise ValueError(f"Unsupported order sort target: {target_column}")

        start, end = self._shifts.get_day_period(shift_date)
        seen: set[int] = set()
        position = 0
        for raw_order_id in ordered_order_ids:
            if raw_order_id is None:
                continue
            try:
                order_id = int(raw_order_id)
            except Exception:
                continue
            if order_id in seen:
                continue
            seen.add(order_id)
            cursor.execute(
                f"""
                UPDATE orders
                SET {target_column} = ?,
                    revision = COALESCE(revision, 0) + 1,
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                  AND admission_id = ?
                  AND datetime >= ? AND datetime < ?
                  AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                """,
                (position, order_id, admission_id, start.isoformat(), end.isoformat()),
            )
            position += 1

    def _resolve_shift_bounds(self, cursor, admission_id: int, shift_date: Optional[datetime]):
        if shift_date is not None:
            return self._shifts.get_day_period(shift_date)
        cursor.execute(
            """
            SELECT MIN(anchor_dt) AS anchor_dt
            FROM (
                SELECT a.planned_time AS anchor_dt
                FROM administrations a
                JOIN orders o ON o.id = a.order_id
                WHERE o.admission_id = ?
                  AND a.is_committed = 0
                UNION ALL
                SELECT datetime AS anchor_dt
                FROM orders
                WHERE admission_id = ?
                  AND (is_committed = 0 OR draft_sort_order IS NOT NULL)
            )
            WHERE anchor_dt IS NOT NULL
            """,
            (admission_id, admission_id),
        )
        row = cursor.fetchone()
        anchor = row["anchor_dt"] if row and row["anchor_dt"] else None
        if anchor:
            try:
                return self._shifts.get_day_period(datetime.fromisoformat(str(anchor).replace(" ", "T")))
            except Exception:
                pass
        return self._shifts.get_day_period(datetime.now())

    def _repair_clearable_draft_integrity(self, cursor, admission_id: int, start: datetime, end: datetime) -> dict[str, int]:
        """
        Чинит старые/сбойные черновики перед откатом.

        Если назначение уже имеет зафиксированные выполнения, его нельзя
        удалять как черновик: SQLite справедливо остановит это внешним ключом.
        В таком случае сохраняем назначение как зафиксированное. Оставшиеся
        чистые черновики удаляются вместе со всеми своими незавершенными
        строками выполнения.
        """
        cursor.execute(
            """
            UPDATE orders
            SET is_committed = 1,
                revision = COALESCE(revision, 0) + 1,
                last_modified_by = COALESCE(last_modified_by, 'doctor'),
                updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
            WHERE is_committed = 0
              AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
              AND admission_id = ?
              AND datetime >= ? AND datetime < ?
              AND EXISTS (
                  SELECT 1
                  FROM administrations committed_admin
                  WHERE committed_admin.order_id = orders.id
                    AND committed_admin.is_committed = 1
              )
            """,
            (admission_id, start.isoformat(), end.isoformat()),
        )
        rescued_orders = int(cursor.rowcount or 0)

        cursor.execute(
            """
            DELETE FROM administrations
            WHERE order_id IN (
                SELECT draft_orders.id
                FROM orders draft_orders
                WHERE draft_orders.is_committed = 0
                  AND draft_orders.admission_id = ?
                  AND draft_orders.datetime >= ? AND draft_orders.datetime < ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM administrations committed_admin
                      WHERE committed_admin.order_id = draft_orders.id
                        AND committed_admin.is_committed = 1
                  )
            )
            """,
            (admission_id, start.isoformat(), end.isoformat()),
        )
        removed_admins = int(cursor.rowcount or 0)
        return {
            "rescued_orders": rescued_orders,
            "removed_admins": removed_admins,
        }

    def repair_draft_integrity(self, admission_id: int, shift_date: Optional[datetime]) -> dict[str, int]:
        with self.dao.db.remcard_transaction() as cursor:
            start, end = self._resolve_shift_bounds(cursor, admission_id, shift_date)
            return self._repair_clearable_draft_integrity(cursor, admission_id, start, end)

    def save_draft_order_sort(
        self,
        admission_id: int,
        shift_date: datetime,
        ordered_order_ids: Sequence[int],
        expected_revisions=None,
    ):
        with self.dao.db.remcard_transaction() as cursor:
            self._assert_order_revisions(cursor, expected_revisions)
            self._apply_order_sort_order(
                cursor,
                admission_id=admission_id,
                shift_date=shift_date,
                ordered_order_ids=ordered_order_ids,
                target_column="draft_sort_order",
            )

    def finalize_card(
        self,
        admission_id: int,
        *,
        shift_date: Optional[datetime] = None,
        ordered_order_ids: Optional[Sequence[int]] = None,
        expected_revisions=None,
    ):
        with self.dao.db.remcard_transaction() as cursor:
            start, end = self._resolve_shift_bounds(cursor, admission_id, shift_date)
            self._assert_order_revisions(cursor, expected_revisions)
            self._apply_order_sort_order(
                cursor,
                admission_id=admission_id,
                shift_date=shift_date,
                ordered_order_ids=ordered_order_ids,
                target_column="draft_sort_order",
            )
            cursor.execute(
                """
                UPDATE orders
                SET sort_order = draft_sort_order,
                    draft_sort_order = NULL,
                    revision = COALESCE(revision, 0) + 1,
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE admission_id = ?
                  AND draft_sort_order IS NOT NULL
                  AND datetime >= ? AND datetime < ?
                """,
                (admission_id, start.isoformat(), end.isoformat()),
            )
            cursor.execute(
                """
                SELECT id
                FROM orders
                WHERE admission_id = ?
                  AND datetime >= ? AND datetime < ?
                  AND (duration_min = -1 OR duration_min >= 61)
                """,
                (admission_id, start.isoformat(), end.isoformat()),
            )
            for row in cursor.fetchall():
                self._domain_service.normalize_order_chain_roles(cursor, int(row["id"]))
            cursor.execute(
                """
                UPDATE administrations SET is_committed = 1, updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE is_committed = 0
                  AND planned_time >= ? AND planned_time < ?
                  AND order_id IN (SELECT id FROM orders WHERE admission_id = ?)
                """,
                (start.isoformat(), end.isoformat(), admission_id),
            )
            cursor.execute(
                """
                UPDATE orders
                SET is_committed = 1,
                    revision = COALESCE(revision, 0) + 1,
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE is_committed = 0
                  AND admission_id = ?
                  AND datetime >= ? AND datetime < ?
                """,
                (admission_id, start.isoformat(), end.isoformat()),
            )
            self._domain_service.sync_transfusions_for_admission(cursor, admission_id)

    def clear_drafts(self, admission_id: int, shift_date: Optional[datetime], expected_revisions=None):
        with self.dao.db.remcard_transaction() as cursor:
            start, end = self._resolve_shift_bounds(cursor, admission_id, shift_date)
            self._assert_order_revisions(cursor, expected_revisions)
            cursor.execute(
                """
                UPDATE orders
                SET draft_sort_order = NULL,
                    revision = COALESCE(revision, 0) + 1,
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE admission_id = ?
                  AND draft_sort_order IS NOT NULL
                  AND datetime >= ? AND datetime < ?
                """,
                (admission_id, start.isoformat(), end.isoformat()),
            )
            cursor.execute(
                """
                DELETE FROM administrations
                WHERE is_committed = 0
                  AND planned_time >= ? AND planned_time < ?
                  AND order_id IN (SELECT id FROM orders WHERE admission_id = ?)
                """,
                (start.isoformat(), end.isoformat(), admission_id),
            )
            cursor.execute(
                """
                UPDATE orders
                SET status = 'active',
                    is_committed = 1,
                    revision = COALESCE(revision, 0) + 1,
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE is_committed = 0
                  AND status = 'deleted'
                  AND admission_id = ?
                  AND datetime >= ? AND datetime < ?
                """,
                (admission_id, start.isoformat(), end.isoformat()),
            )
            self._repair_clearable_draft_integrity(cursor, admission_id, start, end)
            cursor.execute(
                """
                DELETE FROM orders
                WHERE is_committed = 0
                  AND admission_id = ?
                  AND datetime >= ? AND datetime < ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM administrations remaining_admin
                      WHERE remaining_admin.order_id = orders.id
                  )
                """,
                (admission_id, start.isoformat(), end.isoformat()),
            )

    def soft_delete_order_row(self, order_id: int, is_committed: bool, expected_revision: Optional[int] = None):
        with self.dao.db.remcard_transaction() as cursor:
            self._assert_order_revision(cursor, order_id, expected_revision)
            if not is_committed:
                cursor.execute("DELETE FROM administrations WHERE order_id = ?", (order_id,))
                cursor.execute("DELETE FROM orders WHERE id = ?", (order_id,))
                return

            had_committed_admin = self.dao.db.fetch_one_remcard(
                "SELECT 1 FROM administrations WHERE order_id = ? AND is_committed = 1 LIMIT 1",
                (order_id,),
            )
            cursor.execute(
                """
                UPDATE orders
                SET status = 'deleted',
                    is_committed = 0,
                    revision = COALESCE(revision, 0) + 1,
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                """,
                (order_id,),
            )
            query_find = """
                SELECT a.order_id, a.planned_time, a.cell_role, a.volume_ml, a.big_chain_id
                FROM administrations a
                WHERE a.order_id = ? AND COALESCE(a.status, '') != 'deleted'
                  AND a.id IN (
                      SELECT MAX(a2.id)
                      FROM administrations a2
                      WHERE a2.order_id = ?
                      GROUP BY a2.planned_time
                  )
            """
            active_admins = self.dao.db.fetch_all_remcard(query_find, (order_id, order_id))
            for row in active_admins:
                cursor.execute(
                    """
                    INSERT INTO administrations (order_id, big_chain_id, cell_role, planned_time, status, is_committed, volume_ml, updated_at)
                    VALUES (?, ?, ?, ?, 'deleted', 0, ?, STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
                    """,
                    (row["order_id"], row["big_chain_id"], row["cell_role"], row["planned_time"], row["volume_ml"]),
                )

            # Если у назначения нет ни одной committed-ячейки (например, "пустой" препарат),
            # создаем committed tombstone-маркер удаления.
            # Это удерживает запись видимой в режиме only_committed до нажатия "Сохранить"
            # и убирает рассинхронизацию с медсестрой при черновом удалении.
            if not had_committed_admin:
                order_row = self.dao.db.fetch_one_remcard(
                    "SELECT datetime FROM orders WHERE id = ?",
                    (order_id,),
                )
                planned_time = (
                    order_row["datetime"]
                    if order_row and order_row["datetime"]
                    else datetime.now().isoformat()
                )
                cursor.execute(
                    """
                    INSERT INTO administrations (order_id, big_chain_id, cell_role, planned_time, status, is_committed, volume_ml, updated_at)
                    VALUES (?, NULL, 'single', ?, 'deleted', 1, 0, STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
                    """,
                    (order_id, planned_time),
                )

    def clear_all_times(self, admission_id: int, shift_date: datetime):
        start, end = self._shifts.get_day_period(shift_date)
        with self.dao.db.remcard_transaction() as cursor:
            query_find = """
                SELECT a.order_id, a.planned_time, a.cell_role, a.volume_ml, a.big_chain_id
                FROM administrations a
                JOIN orders o ON a.order_id = o.id
                WHERE o.admission_id = ?
                  AND a.planned_time >= ? AND a.planned_time < ?
                  AND COALESCE(a.status, '') != 'deleted'
                  AND a.id IN (
                      SELECT MAX(a2.id) FROM administrations a2 GROUP BY a2.order_id, a2.planned_time
                  )
            """
            active_admins = self.dao.db.fetch_all_remcard(
                query_find,
                (admission_id, start.isoformat(), end.isoformat()),
            )
            for row in active_admins:
                self._domain_service._insert_draft(
                    cursor,
                    row["order_id"],
                    datetime.fromisoformat(row["planned_time"]),
                    "deleted",
                    row["cell_role"],
                    row["big_chain_id"],
                )

    def clear_all_orders(self, admission_id: int, shift_date: datetime, expected_revisions=None):
        with self.dao.db.remcard_transaction() as cursor:
            self._assert_order_revisions(cursor, expected_revisions)
            orders = self.get_orders(admission_id, shift_date)
            active_orders = [o for o in orders if o.status != OrderStatus.DELETED]
            for order in active_orders:
                if order.is_committed == 0:
                    cursor.execute("DELETE FROM administrations WHERE order_id = ?", (order.id,))
                    cursor.execute("DELETE FROM orders WHERE id = ?", (order.id,))
                else:
                    cursor.execute(
                        """
                        UPDATE orders
                        SET status = 'deleted',
                            is_committed = 0,
                            revision = COALESCE(revision, 0) + 1,
                            updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                        WHERE id = ?
                        """,
                        (order.id,),
                    )
                    query_find = """
                        SELECT a.order_id, a.planned_time, a.cell_role, a.volume_ml, a.big_chain_id
                        FROM administrations a
                        WHERE a.order_id = ? AND COALESCE(a.status, '') != 'deleted'
                          AND a.id IN (
                              SELECT MAX(a2.id)
                              FROM administrations a2
                              WHERE a2.order_id = ?
                              GROUP BY a2.planned_time
                          )
                    """
                    active_admins = self.dao.db.fetch_all_remcard(query_find, (order.id, order.id))
                    for row in active_admins:
                        cursor.execute(
                            """
                            INSERT INTO administrations (order_id, big_chain_id, cell_role, planned_time, status, is_committed, volume_ml, updated_at)
                            VALUES (?, ?, ?, ?, 'deleted', 0, ?, STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
                            """,
                            (row["order_id"], row["big_chain_id"], row["cell_role"], row["planned_time"], row["volume_ml"]),
                        )

    def find_recent_orders_source(self, admission_id: int, shift_date: datetime, max_days_back: int = 3):
        for days_back in range(1, max_days_back + 1):
            check_date = shift_date - timedelta(days=days_back)
            orders = self.get_orders(admission_id, check_date, only_committed=True)
            if orders:
                return orders, check_date
        return [], None

    def replace_with_orders_from_date(
        self,
        admission_id: int,
        target_shift_date: datetime,
        source_shift_date: datetime,
        source_orders: List[OrderDTO],
        expected_revisions=None,
    ):
        current_start, current_end = self._shifts.get_day_period(target_shift_date)
        with self.dao.db.remcard_transaction() as cursor:
            self._assert_order_revisions(cursor, expected_revisions)
            current_orders = self.get_orders(admission_id, target_shift_date)
            for order in current_orders:
                if order.status == OrderStatus.DELETED:
                    continue

                if order.is_committed == 0:
                    cursor.execute("DELETE FROM administrations WHERE order_id = ?", (order.id,))
                    cursor.execute("DELETE FROM orders WHERE id = ?", (order.id,))
                else:
                    cursor.execute(
                        """
                        UPDATE orders
                        SET status = 'deleted',
                            is_committed = 0,
                            revision = COALESCE(revision, 0) + 1,
                            updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                        WHERE id = ?
                        """,
                        (order.id,),
                    )
                    active_admins = self.dao.db.fetch_all_remcard(
                        """
                        SELECT order_id, planned_time, cell_role, volume_ml, big_chain_id
                        FROM administrations
                        WHERE order_id = ?
                          AND COALESCE(status, '') != 'deleted'
                          AND id IN (
                              SELECT MAX(id) FROM administrations WHERE order_id = ? GROUP BY planned_time
                          )
                        """,
                        (order.id, order.id),
                    )
                    for row in active_admins:
                        cursor.execute(
                            """
                            INSERT INTO administrations (order_id, big_chain_id, cell_role, planned_time, status, is_committed, volume_ml, updated_at)
                            VALUES (?, ?, ?, ?, 'deleted', 0, ?, STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
                            """,
                            (row["order_id"], row["big_chain_id"], row["cell_role"], row["planned_time"], row["volume_ml"]),
                        )

            old_shift_start, _ = self._shifts.get_day_period(source_shift_date)
            time_diff = current_start - old_shift_start

            for sort_position, src_order in enumerate(source_orders):
                query_order = """
                    INSERT INTO orders (
                        admission_id, datetime, text, drug_key, latin, type, status,
                        dose_value, dose_unit, is_per_kg, frequency, specific_times,
                        rate_ml_h, volume_total, duration_min, sort_order, is_committed,
                        created_at, comment, last_modified_by, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
                """
                new_created_at = current_start.isoformat()
                text_rep = f"{src_order.latin} {src_order.dose_value:g} {src_order.dose_unit}"
                cursor.execute(
                    query_order,
                    (
                        admission_id,
                        new_created_at,
                        text_rep,
                        src_order.drug_key,
                        src_order.latin,
                        src_order.type.value,
                        src_order.status.value,
                        src_order.dose_value,
                        src_order.dose_unit,
                        1 if src_order.is_per_kg else 0,
                        src_order.frequency,
                        json.dumps(src_order.specific_times),
                        src_order.rate_ml_h,
                        src_order.volume_total,
                        src_order.duration_min,
                        sort_position,
                        new_created_at,
                        src_order.comment,
                        "doctor",
                    ),
                )
                new_order_id = cursor.lastrowid
                src_admins = self.dao.db.fetch_all_remcard(
                    """
                    SELECT * FROM administrations
                    WHERE order_id = ?
                      AND COALESCE(status, '') != 'deleted'
                      AND id IN (
                          SELECT MAX(id) FROM administrations WHERE order_id = ? GROUP BY planned_time
                      )
                    """,
                    (src_order.id, src_order.id),
                )
                chain_map = {}
                for src_admin in src_admins:
                    old_chain_id = src_admin["big_chain_id"]
                    new_chain_id = None
                    if old_chain_id:
                        if old_chain_id not in chain_map:
                            chain_map[old_chain_id] = str(uuid.uuid4())
                        new_chain_id = chain_map[old_chain_id]

                    new_time = datetime.fromisoformat(src_admin["planned_time"]) + time_diff
                    if current_start <= new_time < current_end:
                        cursor.execute(
                            """
                            INSERT INTO administrations (order_id, big_chain_id, cell_role, planned_time, status, is_committed, volume_ml, updated_at)
                            VALUES (?, ?, ?, ?, ?, 0, ?, STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
                            """,
                            (
                                new_order_id,
                                new_chain_id,
                                src_admin["cell_role"],
                                new_time.isoformat(),
                                src_admin["status"],
                                src_admin["volume_ml"],
                            ),
                        )

    def apply_left_click(self, order: OrderDTO, admin, planned_time: datetime):
        self._domain_service.handle_left_click(order, admin, planned_time)

    def apply_middle_click(self, order: OrderDTO, admin, planned_time: datetime):
        self._domain_service.handle_middle_click(order, admin, planned_time)

    def apply_right_click(self, order: OrderDTO, admin, planned_time: datetime):
        self._domain_service.handle_right_click(order, admin, planned_time)

    def set_nurse_status(self, admin_id: int, mark: str, performer_id: Optional[int] = None):
        self._domain_service.set_nurse_status(admin_id, mark, performer_id=performer_id)

    def cancel_nurse_action(self, admin_id: int):
        self._domain_service.cancel_nurse_action(admin_id)

    def set_doctor_status(self, admin_id: int, mark: str, performer_id: Optional[int] = None):
        self._domain_service.set_doctor_status(admin_id, mark, performer_id=performer_id)

    def cancel_doctor_action(self, admin_id: int):
        self._domain_service.cancel_doctor_action(admin_id)

    def get_nurse_orders_data(self, admission_id: int, shift_date: datetime):
        return self._domain_service.get_nurse_orders_data(admission_id, shift_date)

    def get_upcoming_orders_across_active_admissions(self, shift_date: datetime):
        return self._domain_service.get_upcoming_orders_across_active_admissions(shift_date)

    def get_nurse_statistics_rows(self, admission_ids: Sequence[int]):
        if not admission_ids:
            return []
        placeholders = ",".join("?" for _ in admission_ids)
        query = f"""
            SELECT a.planned_time, a.actual_time, a.comment, a.status as admin_status, a.cell_role,
                   o.admission_id, o.drug_key, o.text, o.latin, o.dose_value, o.dose_unit
            FROM administrations a
            JOIN orders o ON a.order_id = o.id
            WHERE o.admission_id IN ({placeholders})
              AND a.id IN (
                  SELECT MAX(id)
                  FROM administrations
                  GROUP BY order_id, planned_time
              )
              AND COALESCE(a.status, '') != 'deleted'
              AND COALESCE(o.status, '') != 'deleted'
        """
        return self.dao.db.fetch_all_remcard(query, tuple(admission_ids))
