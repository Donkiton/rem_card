import json
from typing import List, Optional
from datetime import datetime, timedelta
from ..dto.remcard_dto import OrderDTO, OrderType, OrderStatus
from .sync_cursor import is_cursor_newer, make_sync_cursor, normalize_sync_cursor

class OrdersDAO:
    def __init__(self, db_manager):
        self.db = db_manager

    def _build_visible_orders_query(
        self,
        admission_id: int,
        date: Optional[datetime] = None,
        only_committed: bool = False,
        *,
        select_clause: str = "*",
    ):
        if date:
            start_dt = date.replace(hour=8, minute=0, second=0, microsecond=0)
            if date.hour < 8:
                start_dt -= timedelta(days=1)
            end_dt = start_dt + timedelta(days=1)

            if only_committed:
                # Специальная логика для медсестры и печати:
                # - показываем сохраненные активные назначения
                # - плюс записи с черновиком, у которых есть коммиченные administrations
                query = f"""
                    SELECT {select_clause} FROM orders
                    WHERE admission_id = ?
                    AND datetime >= ? AND datetime < ?
                    AND (
                        (is_committed = 1 AND COALESCE(status, '') NOT IN ('deleted', 'cancelled'))
                        OR
                        (is_committed = 0 AND EXISTS (
                            SELECT 1
                            FROM administrations a
                            WHERE a.order_id = orders.id
                              AND a.is_committed = 1
                              AND COALESCE(a.status, '') NOT IN ('deleted', 'cancelled')
                        ))
                    )
                """
            else:
                # Логика для врача: показываем активные/черновые, скрываем удаленные.
                query = f"""
                    SELECT {select_clause} FROM orders
                    WHERE admission_id = ?
                    AND datetime >= ? AND datetime < ?
                    AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                """
            params = [admission_id, start_dt.isoformat(), end_dt.isoformat()]
        else:
            if only_committed:
                query = f"""
                    SELECT {select_clause} FROM orders
                    WHERE admission_id = ?
                    AND (
                        (is_committed = 1 AND COALESCE(status, '') != 'deleted')
                        OR
                        (is_committed = 0 AND EXISTS (
                            SELECT 1
                            FROM administrations a
                            WHERE a.order_id = orders.id
                              AND a.is_committed = 1
                              AND COALESCE(a.status, '') NOT IN ('deleted', 'cancelled')
                        ))
                    )
                """
            else:
                query = f"SELECT {select_clause} FROM orders WHERE admission_id = ? AND COALESCE(status, '') != 'deleted'"
            params = [admission_id]

        if only_committed:
            query += " ORDER BY COALESCE(sort_order, 0) ASC, created_at ASC, id ASC "
        else:
            query += " ORDER BY COALESCE(draft_sort_order, sort_order, 0) ASC, created_at ASC, id ASC "
        return query, tuple(params)

    def get_next_sort_order(self, admission_id: int, date: Optional[datetime] = None) -> int:
        if date:
            start_dt = date.replace(hour=8, minute=0, second=0, microsecond=0)
            if date.hour < 8:
                start_dt -= timedelta(days=1)
            end_dt = start_dt + timedelta(days=1)
            row = self.db.fetch_one_remcard(
                """
                SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_sort_order
                FROM orders
                WHERE admission_id = ?
                  AND datetime >= ? AND datetime < ?
                  AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                """,
                (admission_id, start_dt.isoformat(), end_dt.isoformat()),
            )
        else:
            row = self.db.fetch_one_remcard(
                """
                SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_sort_order
                FROM orders
                WHERE admission_id = ?
                  AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                """,
                (admission_id,),
            )
        return int(row["next_sort_order"] if row and row["next_sort_order"] is not None else 0)

    def add_order(self, dto: OrderDTO):
        query = """
            INSERT INTO orders (
                admission_id, datetime, text, drug_key, latin, type, status, 
                dose_value, dose_unit, is_per_kg, 
                frequency, specific_times, 
                rate_ml_h, volume_total, duration_min, sort_order,
                is_committed, created_at, comment,
                last_modified_by, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
        """
        text_rep = f"{dto.latin} {dto.dose_value:g} {dto.dose_unit}"
        
        last_modified_by = dto.last_modified_by if dto.last_modified_by else 'doctor'
        
        res = self.db.execute_remcard(query, (
            dto.admission_id, dto.created_at.isoformat(), text_rep, dto.drug_key, dto.latin, dto.type.value, dto.status.value,
            dto.dose_value, dto.dose_unit, 1 if dto.is_per_kg else 0,
            dto.frequency, json.dumps(dto.specific_times),
            dto.rate_ml_h, dto.volume_total, dto.duration_min, dto.sort_order,
            dto.is_committed,
            dto.created_at.isoformat(), dto.comment, last_modified_by
        ))
        order_id = getattr(res, "lastrowid", None)

        from rem_card.app.logger import logger

        if not order_id:
            # Возможен режим deferred-write через durable outbox (lastrowid = None).
            # Это не "фатал", поэтому не шумим ложной критикой.
            op_id = getattr(res, "op_id", None)
            if op_id:
                logger.warning(
                    "Order write deferred to outbox (admission_id=%s, op_id=%s). "
                    "Immediate ID is unavailable until replay.",
                    dto.admission_id,
                    op_id,
                )
            else:
                logger.warning(
                    "Order insert returned empty lastrowid (admission_id=%s, latin=%s).",
                    dto.admission_id,
                    dto.latin,
                )
            dto.id = None
            return None

        # Верификацию выполняем по central-conn (а не через local replica),
        # чтобы не ловить ложные срабатывания из-за лагов реплики.
        central_fetch = getattr(self.db, "_fetch_one_central", None)
        if callable(central_fetch):
            check_row = central_fetch("SELECT id FROM orders WHERE id=?", (order_id,))
        else:
            check_row = self.db.fetch_one_remcard("SELECT id FROM orders WHERE id=?", (order_id,))

        if not check_row:
            logger.error(
                "Order %s insert did not pass immediate central verification. "
                "Will rely on subsequent polling/checkpoints.",
                order_id,
            )
        else:
            logger.debug("Order %s saved and central verification passed.", order_id)

        dto.id = order_id
        return order_id

    def get_orders(self, admission_id: int, date: Optional[datetime] = None, only_committed: bool = False) -> List[OrderDTO]:
        query, params = self._build_visible_orders_query(
            admission_id=admission_id,
            date=date,
            only_committed=only_committed,
            select_clause="*",
        )
        rows = self.db.fetch_all_remcard(query, params)
        
        orders = []
        for r in rows:
            rd = dict(r)
            orders.append(OrderDTO(
                id=rd['id'],
                admission_id=rd['admission_id'],
                drug_key=rd['drug_key'],
                latin=rd['latin'],
                type=OrderType(rd['type']),
                status=OrderStatus(rd['status']),
                dose_value=rd['dose_value'],
                dose_unit=rd['dose_unit'],
                is_per_kg=bool(rd['is_per_kg']),
                frequency=rd['frequency'],
                specific_times=json.loads(rd['specific_times'] or "[]"),
                rate_ml_h=rd.get('rate_ml_h'),
                volume_total=rd.get('volume_total'),
                duration_min=rd.get('duration_min'),
                sort_order=rd.get('sort_order', 0) or 0,
                draft_sort_order=rd.get('draft_sort_order'),
                is_finalized=bool(rd.get('is_finalized', 0)),
                is_committed=rd.get('is_committed', 0),
                revision=rd.get('revision', 0) or 0,
                created_at=datetime.fromisoformat(rd['created_at']),
                comment=rd['comment'],
                last_modified_by=rd.get('last_modified_by'),
                updated_at=rd.get('updated_at')
            ))
        return orders

    def get_order_ids(self, admission_id: int, date: Optional[datetime] = None, only_committed: bool = False) -> List[int]:
        query, params = self._build_visible_orders_query(
            admission_id=admission_id,
            date=date,
            only_committed=only_committed,
            select_clause="id",
        )
        rows = self.db.fetch_all_remcard(query, params)
        return [int(r["id"]) for r in rows if r and r["id"] is not None]

    def fetch_updated_orders(self, admission_id: int, last_sync_time, only_committed: bool = False):
        last_sync_ts, last_sync_id = normalize_sync_cursor(last_sync_time)
        query = """
            SELECT *
            FROM orders
            WHERE admission_id = ?
              AND (
                  COALESCE(STRFTIME('%Y-%m-%d %H:%M:%f', updated_at), '') > ?
                  OR (
                      COALESCE(STRFTIME('%Y-%m-%d %H:%M:%f', updated_at), '') = ?
                      AND id > ?
                  )
              )
        """
        params = [admission_id, last_sync_ts, last_sync_ts, last_sync_id]
        if only_committed:
            query += " AND is_committed = 1 "
        query += " ORDER BY COALESCE(STRFTIME('%Y-%m-%d %H:%M:%f', updated_at), '') ASC, id ASC"
        rows = self.db.fetch_all_remcard(query, tuple(params))
        
        orders = []
        new_sync_cursor = make_sync_cursor(last_sync_ts, last_sync_id)
        
        if rows:
            for r in rows:
                rd = dict(r)
                row_updated_at = str(rd.get('updated_at') or new_sync_cursor["updated_at"])
                row_id = int(rd.get('id') or 0)
                if is_cursor_newer(row_updated_at, row_id, new_sync_cursor["updated_at"], new_sync_cursor["id"]):
                    new_sync_cursor = make_sync_cursor(row_updated_at, row_id)
                orders.append(OrderDTO(
                    id=rd['id'],
                    admission_id=rd['admission_id'],
                    drug_key=rd['drug_key'],
                    latin=rd['latin'],
                    type=OrderType(rd['type']),
                    status=OrderStatus(rd['status']),
                    dose_value=rd['dose_value'],
                    dose_unit=rd['dose_unit'],
                    is_per_kg=bool(rd['is_per_kg']),
                    frequency=rd['frequency'],
                    specific_times=json.loads(rd['specific_times'] or "[]"),
                    rate_ml_h=rd.get('rate_ml_h'),
                    volume_total=rd.get('volume_total'),
                    duration_min=rd.get('duration_min'),
                    sort_order=rd.get('sort_order', 0) or 0,
                    draft_sort_order=rd.get('draft_sort_order'),
                    is_finalized=bool(rd.get('is_finalized', 0)),
                    is_committed=rd.get('is_committed', 0),
                    revision=rd.get('revision', 0) or 0,
                    created_at=datetime.fromisoformat(rd['created_at']),
                    comment=rd['comment'],
                    last_modified_by=rd.get('last_modified_by'),
                    updated_at=rd.get('updated_at')
                ))
        if isinstance(last_sync_time, dict):
            return orders, new_sync_cursor
        return orders, new_sync_cursor["updated_at"]

    def delete_all_for_admission(self, admission_id: int):
        self.db.execute_remcard("DELETE FROM orders WHERE admission_id = ?", (admission_id,))

    def get_all_dates(self, admission_id: int) -> List[datetime]:
        """Возвращает все уникальные даты для назначений пациента."""
        query = "SELECT DISTINCT date(datetime) as d FROM orders WHERE admission_id = ? AND COALESCE(status, '') != 'deleted' ORDER BY d ASC"
        rows = self.db.fetch_all_remcard(query, (admission_id,))
        dates = []
        for r in rows:
            if r['d']:
                dt = datetime.strptime(r['d'], '%Y-%m-%d').replace(hour=12, minute=0)
                dates.append(dt)
        return dates

    def soft_delete_order(self, order_id: int):
        """Мягкое удаление заказа."""
        # Просто ставим status = 'deleted' и is_committed = 0
        self.db.execute_remcard("""
            UPDATE orders 
            SET status = 'deleted',
                is_committed = 0,
                revision = COALESCE(revision, 0) + 1,
                updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
            WHERE id = ?
        """, (order_id,))
        
        # Также все связанные ячейки в 'deleted'
        self.db.execute_remcard("""
            UPDATE administrations 
            SET status = 'deleted', is_committed = 0, updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
            WHERE id IN (SELECT id FROM administrations WHERE order_id = ?)
        """, (order_id,))

    def soft_delete_all_administrations_for_shift(self, admission_id: int, start_iso: str, end_iso: str):
        """Помечает все администрации смены как удаленные."""
        self.db.execute_remcard("""
            UPDATE administrations 
            SET status = 'deleted', is_committed = 0, updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
            WHERE planned_time >= ? AND planned_time < ?
              AND order_id IN (SELECT id FROM orders WHERE admission_id = ?)
        """, (start_iso, end_iso, admission_id))

    def soft_delete_all_for_shift(self, admission_id: int, start_iso: str, end_iso: str):
        """Помечает все назначения и администрации смены как удаленные."""
        self.db.execute_remcard("""
            UPDATE orders 
            SET status = 'deleted',
                is_committed = 0,
                revision = COALESCE(revision, 0) + 1,
                updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
            WHERE admission_id = ?
              AND datetime >= ? AND datetime < ?
        """, (admission_id, start_iso, end_iso))
        
        self.soft_delete_all_administrations_for_shift(admission_id, start_iso, end_iso)

    def update_status(self, order_id: int, status: str, last_modified_by: str = 'doctor', current_updated_at: Optional[str] = None):
        query = """
            UPDATE orders 
            SET status = ?,
                last_modified_by = ?,
                revision = COALESCE(revision, 0) + 1,
                updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
            WHERE id = ?
        """
        params = [status, last_modified_by, order_id]
        if current_updated_at:
            query += " AND updated_at = ?"
            params.append(current_updated_at)
        self.db.execute_remcard(query, tuple(params))
