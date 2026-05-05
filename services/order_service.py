import json
import uuid
from datetime import datetime, timedelta
from typing import List, Mapping, Optional, Sequence

from rem_card.data.dao.sync_cursor import normalize_sync_cursor
from rem_card.data.dto.remcard_dto import OrderStatus
from rem_card.services.order_domain_service import OrderDomainService
from rem_card.services.shift_service import ShiftService

from ..data.dto.remcard_dto import OrderDTO
from ..data.dao.orders_dao import OrdersDAO


ORDER_CONFLICT_MESSAGE = "Данные изменены другим рабочим местом. Обновите карточку."


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

    def add_order(self, dto: OrderDTO):
        with self.dao.db.remcard_transaction():
            self._assign_next_sort_order_if_needed(dto)
            return self.dao.add_order(dto)

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
        return self.dao.db.fetch_all_remcard(query, tuple(final_params))

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
            cursor.execute(
                """
                DELETE FROM orders
                WHERE is_committed = 0
                  AND admission_id = ?
                  AND datetime >= ? AND datetime < ?
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
            orders = self.get_orders(admission_id, check_date)
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

    def get_nurse_orders_data(self, admission_id: int, shift_date: datetime):
        return self._domain_service.get_nurse_orders_data(admission_id, shift_date)

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
