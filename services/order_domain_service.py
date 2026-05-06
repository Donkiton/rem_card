import os
import json
import uuid
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import List, Optional, Tuple, Dict

from ..data.dto.remcard_dto import OrderDTO, AdministrationDTO, OrderType, OrderStatus
from ..data.dao.db_manager import DatabaseManager
from rem_card.app.logger import logger
from rem_card.services.orders_sync_observability import record_orders_sync_event

# Метки выполнения медсестрой (хранятся в поле comment)
NURSE_MARK_EXECUTED = "nurse_executed"
NURSE_MARK_NOT_EXECUTED = "nurse_not_executed"
REMCARD_TRANSFUSION_SOURCE = "remcard_order"
TRANSFUSION_DRUG_KEYS = {"blood", "plasma"}
LEGACY_STATUS_SANITIZE_META_KEY = "legacy_admin_statuses_sanitized_v1"
LEGACY_STATUS_SANITIZE_RETRY_SEC = 30.0
_INHERIT_CHAIN_ID = object()


class OrderDomainService:
    _sanitize_guard = threading.Lock()
    _legacy_statuses_sanitized_global = False
    _legacy_statuses_retry_not_before = 0.0
    _seed_cache_lock = threading.Lock()
    _seed_cache: Dict[str, tuple[float, Dict]] = {}

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self._legacy_statuses_sanitized = False

    @staticmethod
    def _is_retryable_lock_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return (
            "database is locked" in msg
            or "database table is locked" in msg
            or "database schema is locked" in msg
            or "could not acquire sequential write lock" in msg
        )

    def _sanitize_legacy_statuses_once(self, allow_write: bool = True):
        """
        Единоразовая санация старых некорректных статусов.
        Не должна выполняться на каждом read-поллинге.
        """
        cls = self.__class__
        if self._legacy_statuses_sanitized or cls._legacy_statuses_sanitized_global:
            self._legacy_statuses_sanitized = True
            return

        now_mono = time.monotonic()
        if now_mono < cls._legacy_statuses_retry_not_before:
            return

        with cls._sanitize_guard:
            if self._legacy_statuses_sanitized or cls._legacy_statuses_sanitized_global:
                self._legacy_statuses_sanitized = True
                return

            now_mono = time.monotonic()
            if now_mono < cls._legacy_statuses_retry_not_before:
                return

            try:
                row = self.db.fetch_one_remcard(
                    "SELECT value FROM meta WHERE key = ?",
                    (LEGACY_STATUS_SANITIZE_META_KEY,),
                )
                if row and str(row[0]) == "1":
                    self._legacy_statuses_sanitized = True
                    cls._legacy_statuses_sanitized_global = True
                    cls._legacy_statuses_retry_not_before = 0.0
                    return
            except sqlite3.OperationalError as exc:
                if self._is_retryable_lock_error(exc):
                    cls._legacy_statuses_retry_not_before = time.monotonic() + LEGACY_STATUS_SANITIZE_RETRY_SEC
                    logger.debug("Legacy statuses sanitize read postponed due to lock: %s", exc)
                    return
                logger.warning("Legacy statuses sanitize read failed: %s", exc)
                cls._legacy_statuses_retry_not_before = time.monotonic() + LEGACY_STATUS_SANITIZE_RETRY_SEC
                return
            except Exception as exc:
                logger.warning("Legacy statuses sanitize read failed: %s", exc)
                cls._legacy_statuses_retry_not_before = time.monotonic() + LEGACY_STATUS_SANITIZE_RETRY_SEC
                return

            if not allow_write:
                return

            try:
                changed_rows = 0
                with self.db.remcard_transaction(source="sanitize_legacy_admin_statuses_once") as cursor:
                    cursor.execute(
                        "UPDATE administrations SET status = 'planned' WHERE status IN ('executed', 'not_executed', 'overdue_not_executed')"
                    )
                    changed_rows = int(cursor.rowcount or 0)
                    cursor.execute(
                        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                        (LEGACY_STATUS_SANITIZE_META_KEY, "1"),
                    )
                self._legacy_statuses_sanitized = True
                cls._legacy_statuses_sanitized_global = True
                cls._legacy_statuses_retry_not_before = 0.0
                if changed_rows > 0:
                    logger.info("Legacy administrations status sanitize completed: %s row(s) normalized", changed_rows)
            except sqlite3.OperationalError as exc:
                if self._is_retryable_lock_error(exc):
                    cls._legacy_statuses_retry_not_before = time.monotonic() + LEGACY_STATUS_SANITIZE_RETRY_SEC
                    logger.warning("Legacy statuses sanitize postponed due to lock: %s", exc)
                    return
                logger.warning("Legacy statuses sanitize failed: %s", exc)
                cls._legacy_statuses_retry_not_before = time.monotonic() + LEGACY_STATUS_SANITIZE_RETRY_SEC
            except Exception as exc:
                logger.warning("Legacy statuses sanitize failed: %s", exc)
                cls._legacy_statuses_retry_not_before = time.monotonic() + LEGACY_STATUS_SANITIZE_RETRY_SEC

    def _get_last_admin(self, order_id: int, planned_time: datetime) -> Optional[dict]:
        """Возвращает последнюю (Source of Truth) запись для ячейки."""
        return self.db.fetch_one_remcard(
            "SELECT * FROM administrations WHERE order_id = ? AND planned_time = ? ORDER BY id DESC LIMIT 1",
            (order_id, planned_time.isoformat()),
        )

    @staticmethod
    def _is_uncommitted_admin(row) -> bool:
        try:
            return int(row["is_committed"] or 0) == 0
        except Exception:
            return False

    @staticmethod
    def _admin_matches_committed_baseline(latest, committed) -> bool:
        if latest is None or committed is None:
            return False
        latest_status = str(latest["status"] or "")
        committed_status = str(committed["status"] or "")
        if latest_status != committed_status:
            return False
        if str(latest["cell_role"] or "") != str(committed["cell_role"] or ""):
            return False
        if str(latest["big_chain_id"] or "") != str(committed["big_chain_id"] or ""):
            return False
        # comment/actual_time/performer_id belong to nurse execution marks, not
        # to the doctor's draft cell shape. A nurse may update the committed
        # baseline while a doctor has a delete draft, and restoring the cell
        # must collapse back to that committed baseline instead of leaving a
        # false dirty draft.
        try:
            return float(latest["volume_ml"] or 0.0) == float(committed["volume_ml"] or 0.0)
        except Exception:
            return True

    @staticmethod
    def _row_dt(row) -> datetime:
        return datetime.fromisoformat(str(row["planned_time"]))

    @staticmethod
    def _is_planned_row(row) -> bool:
        return str(row["status"] or "") == "planned"

    def _fetch_planned_chain_segment(
        self,
        cursor,
        order_id: int,
        big_chain_id: Optional[str],
        planned_time: datetime,
    ) -> List[dict]:
        if not big_chain_id:
            return []
        cursor.execute(
            """
            SELECT *
            FROM administrations a
            WHERE a.order_id = ?
              AND a.big_chain_id = ?
              AND a.status = 'planned'
              AND a.id = (
                  SELECT MAX(a2.id)
                  FROM administrations a2
                  WHERE a2.order_id = a.order_id
                    AND a2.planned_time = a.planned_time
              )
            ORDER BY a.planned_time ASC
            """,
            (int(order_id), big_chain_id),
        )
        rows = [
            self._cursor_row_to_dict(cursor, row)
            for row in cursor.fetchall()
        ]
        rows = [row for row in rows if row and row.get("planned_time")]
        planned_key = planned_time.isoformat()
        center = next(
            (idx for idx, row in enumerate(rows) if str(row["planned_time"]) == planned_key),
            None,
        )
        if center is None:
            return []

        left = center
        while left > 0:
            current_dt = self._row_dt(rows[left])
            previous_dt = self._row_dt(rows[left - 1])
            if current_dt - previous_dt != timedelta(hours=1):
                break
            left -= 1

        right = center
        while right + 1 < len(rows):
            current_dt = self._row_dt(rows[right])
            next_dt = self._row_dt(rows[right + 1])
            if next_dt - current_dt != timedelta(hours=1):
                break
            right += 1

        return rows[left:right + 1]

    def _retire_chain_tail(
        self,
        cursor,
        segment: List[dict],
        planned_time: datetime,
    ) -> None:
        planned_key = planned_time.isoformat()
        for row in segment:
            if str(row["planned_time"]) <= planned_key:
                continue
            self._insert_draft(
                cursor,
                row["order_id"],
                datetime.fromisoformat(row["planned_time"]),
                "deleted",
                row["cell_role"],
                row["big_chain_id"],
                last_row=row,
            )

    def _retire_chain_except(
        self,
        cursor,
        segment: List[dict],
        planned_time: datetime,
    ) -> None:
        planned_key = planned_time.isoformat()
        for row in segment:
            if str(row["planned_time"]) == planned_key:
                continue
            self._insert_draft(
                cursor,
                row["order_id"],
                datetime.fromisoformat(row["planned_time"]),
                "deleted",
                row["cell_role"],
                row["big_chain_id"],
                last_row=row,
            )

    def _promote_previous_chain_end(
        self,
        cursor,
        segment: List[dict],
        planned_time: datetime,
    ) -> None:
        previous_rows = [
            row
            for row in segment
            if str(row["planned_time"]) < planned_time.isoformat()
        ]
        if not previous_rows:
            return
        prev_row = previous_rows[-1]
        new_role = "single" if len(previous_rows) == 1 else "end"
        self._insert_draft(
            cursor,
            prev_row["order_id"],
            datetime.fromisoformat(prev_row["planned_time"]),
            prev_row["status"],
            new_role,
            prev_row["big_chain_id"],
            last_row=prev_row,
        )

    def normalize_order_chain_roles(self, cursor, order_id: int) -> None:
        cursor.execute(
            """
            SELECT *
            FROM administrations a
            WHERE a.order_id = ?
              AND a.status = 'planned'
              AND a.cell_role IN ('start', 'body', 'end', 'single')
              AND a.id = (
                  SELECT MAX(a2.id)
                  FROM administrations a2
                  WHERE a2.order_id = a.order_id
                    AND a2.planned_time = a.planned_time
              )
            ORDER BY COALESCE(a.big_chain_id, ''), a.planned_time ASC
            """,
            (int(order_id),),
        )
        rows = [
            self._cursor_row_to_dict(cursor, row)
            for row in cursor.fetchall()
        ]
        rows = [row for row in rows if row and row.get("planned_time")]
        if not rows:
            return

        grouped: Dict[str, List[dict]] = {}
        for row in rows:
            chain_id = row.get("big_chain_id")
            if chain_id:
                group_key = f"chain:{chain_id}"
            elif str(row.get("cell_role") or "") == "single":
                group_key = f"single:{row['planned_time']}"
            else:
                group_key = "chain:"
            grouped.setdefault(group_key, []).append(row)

        for group_rows in grouped.values():
            group_rows.sort(key=lambda row: str(row["planned_time"]))
            segment: List[dict] = []
            previous_dt: Optional[datetime] = None
            for row in group_rows:
                current_dt = self._row_dt(row)
                if previous_dt is not None and current_dt - previous_dt != timedelta(hours=1):
                    self._normalize_chain_segment(cursor, segment)
                    segment = []
                segment.append(row)
                previous_dt = current_dt
            self._normalize_chain_segment(cursor, segment)

    def _normalize_chain_segment(self, cursor, segment: List[dict]) -> None:
        if not segment:
            return
        expected_chain_id = None if len(segment) == 1 else (segment[0].get("big_chain_id") or str(uuid.uuid4()))
        last_index = len(segment) - 1
        for idx, row in enumerate(segment):
            if len(segment) == 1:
                expected_role = "single"
            elif idx == 0:
                expected_role = "start"
            elif idx == last_index:
                expected_role = "end"
            else:
                expected_role = "body"

            if (
                str(row.get("cell_role") or "") == expected_role
                and str(row.get("big_chain_id") or "") == str(expected_chain_id or "")
            ):
                continue

            self._insert_draft(
                cursor,
                row["order_id"],
                datetime.fromisoformat(row["planned_time"]),
                "planned",
                expected_role,
                expected_chain_id,
                last_row=row,
                comment=row.get("comment"),
            )

    def _collapse_noop_cell_draft(self, cursor, order_id: int, planned_time: datetime) -> None:
        """
        Убирает черновую историю, если последний draft возвращает ячейку к
        исходному committed-состоянию. Это закрывает быстрый сценарий
        поставить -> убрать и убрать -> поставить без ложной активной кнопки
        сохранения.
        """
        planned_key = planned_time.isoformat()
        cursor.execute(
            """
            SELECT *
            FROM administrations
            WHERE order_id = ? AND planned_time = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (order_id, planned_key),
        )
        latest = cursor.fetchone()
        if latest is None or not self._is_uncommitted_admin(latest):
            return

        cursor.execute(
            """
            SELECT *
            FROM administrations
            WHERE order_id = ? AND planned_time = ? AND is_committed = 1
            ORDER BY id DESC
            LIMIT 1
            """,
            (order_id, planned_key),
        )
        committed = cursor.fetchone()

        status = str(latest["status"] or "")
        is_noop_without_baseline = committed is None and status in ("deleted", "cancelled")
        is_noop_against_baseline = self._admin_matches_committed_baseline(latest, committed)
        if not is_noop_without_baseline and not is_noop_against_baseline:
            return

        cursor.execute(
            """
            DELETE FROM administrations
            WHERE order_id = ? AND planned_time = ? AND is_committed = 0
            """,
            (order_id, planned_key),
        )

    def _get_last_admins_for_times(self, cursor, order_id: int, planned_times: List[datetime]) -> Dict[str, dict]:
        if not planned_times:
            return {}
        planned_keys = [dt.isoformat() for dt in planned_times]
        placeholders = ",".join("?" for _ in planned_keys)
        cursor.execute(
            f"""
            SELECT *
            FROM administrations a
            WHERE a.order_id = ?
              AND a.planned_time IN ({placeholders})
              AND a.id = (
                  SELECT MAX(a2.id)
                  FROM administrations a2
                  WHERE a2.order_id = a.order_id
                    AND a2.planned_time = a.planned_time
              )
            """,
            (int(order_id), *planned_keys),
        )
        rows = cursor.fetchall()
        result: Dict[str, dict] = {}
        for row in rows:
            mapped = self._cursor_row_to_dict(cursor, row)
            if mapped and mapped.get("planned_time"):
                result[str(mapped["planned_time"])] = mapped
        return result

    def _insert_draft(
        self,
        cursor,
        order_id: int,
        planned_time: datetime,
        status: str,
        cell_role: str,
        big_chain_id=_INHERIT_CHAIN_ID,
        last_row: Optional[dict] = None,
        comment: Optional[str] = None,
    ):
        """
        Для любых изменений в черновике всегда делаем INSERT.
        Это обеспечивает корректную работу rollback для "последней версии" ячейки.
        """
        if last_row is None:
            last_row = self._get_last_admin(order_id, planned_time)
        volume = last_row["volume_ml"] if last_row else 0.0

        # Если big_chain_id явно не передан, наследуем его у предыдущей версии ячейки.
        effective_chain_id = (
            last_row["big_chain_id"]
            if big_chain_id is _INHERIT_CHAIN_ID and last_row
            else (None if big_chain_id is _INHERIT_CHAIN_ID else big_chain_id)
        )

        cursor.execute(
            """
            INSERT INTO administrations (
                order_id, big_chain_id, cell_role, planned_time, status, is_committed, comment, volume_ml, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 0, ?, ?, STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
            """,
            (order_id, effective_chain_id, cell_role, planned_time.isoformat(), status, comment, volume),
        )
        self._collapse_noop_cell_draft(cursor, order_id, planned_time)

    def handle_left_click(self, order: OrderDTO, admin: Optional[AdministrationDTO], planned_time: datetime):
        with self.db.remcard_transaction() as cursor:
            last_row = self._get_last_admin(order.id, planned_time)
            is_long = (order.duration_min is not None and order.duration_min >= 61) or order.duration_min == -1

            if not last_row:
                if is_long:
                    self._create_chain(cursor, order, planned_time)
                    self.normalize_order_chain_roles(cursor, order.id)
                else:
                    self._insert_draft(cursor, order.id, planned_time, "planned", "single", last_row=last_row)
                return

            status = last_row["status"]
            role = last_row["cell_role"]
            big_chain_id = last_row["big_chain_id"]

            if status == "planned":
                if is_long:
                    segment = self._fetch_planned_chain_segment(cursor, order.id, big_chain_id, planned_time)
                    if role == "start":
                        self._retire_chain_except(cursor, segment, planned_time)
                        self._insert_draft(cursor, order.id, planned_time, "deleted", role, big_chain_id, last_row=last_row)
                    elif role == "body":
                        self._insert_draft(cursor, order.id, planned_time, "planned", "end", big_chain_id, last_row=last_row)
                        self._retire_chain_tail(cursor, segment, planned_time)
                    elif role == "single":
                        self._insert_draft(cursor, order.id, planned_time, "deleted", "single", big_chain_id, last_row=last_row)
                    elif role == "end":
                        self._insert_draft(cursor, order.id, planned_time, "deleted", "single", big_chain_id, last_row=last_row)
                        self._promote_previous_chain_end(cursor, segment, planned_time)
                else:
                    self._insert_draft(cursor, order.id, planned_time, "deleted", "single", last_row=last_row)

            elif status in ("cancelled", "deleted"):
                if is_long:
                    self._create_chain(cursor, order, planned_time)
                else:
                    self._insert_draft(cursor, order.id, planned_time, "planned", "single", last_row=last_row)

            if is_long:
                self.normalize_order_chain_roles(cursor, order.id)

    def handle_middle_click(self, order: OrderDTO, admin: Optional[AdministrationDTO], planned_time: datetime):
        with self.db.remcard_transaction() as cursor:
            last_row = self._get_last_admin(order.id, planned_time)
            if not last_row:
                return
            is_long = (order.duration_min is not None and order.duration_min >= 61) or order.duration_min == -1

            status = last_row["status"]
            role = last_row["cell_role"]
            big_chain_id = last_row["big_chain_id"]

            if status == "planned":
                segment = self._fetch_planned_chain_segment(cursor, order.id, big_chain_id, planned_time)
                if role == "start":
                    self._insert_draft(cursor, order.id, planned_time, "cancelled", role, big_chain_id, last_row=last_row)
                    self._retire_chain_except(cursor, segment, planned_time)
                elif role == "body":
                    self._insert_draft(cursor, order.id, planned_time, "planned", "end", big_chain_id, last_row=last_row)
                    self._retire_chain_tail(cursor, segment, planned_time)
                elif role == "end":
                    self._insert_draft(cursor, order.id, planned_time, "cancelled", "single", big_chain_id, last_row=last_row)
                    self._promote_previous_chain_end(cursor, segment, planned_time)
                else:
                    self._insert_draft(cursor, order.id, planned_time, "cancelled", "single", last_row=last_row)
            elif status == "cancelled":
                self._insert_draft(cursor, order.id, planned_time, "deleted", last_row["cell_role"], big_chain_id, last_row=last_row)

            if is_long:
                self.normalize_order_chain_roles(cursor, order.id)

    def handle_right_click(self, order: OrderDTO, admin: Optional[AdministrationDTO], planned_time: datetime):
        pass

    # --- Nurse Methods (Sector 1a / 5) ---

    @staticmethod
    def _cursor_row_to_dict(cursor, row) -> Optional[dict]:
        if not row:
            return None
        if hasattr(row, "keys"):
            return dict(row)
        return {desc[0]: row[idx] for idx, desc in enumerate(cursor.description or [])}

    def _fetch_admin_for_nurse_update(self, cursor, admin_id: int) -> Optional[dict]:
        cursor.execute(
            """
            SELECT
                a.*,
                o.admission_id AS admission_id,
                o.status AS order_status,
                o.updated_at AS order_updated_at
            FROM administrations a
            JOIN orders o ON o.id = a.order_id
            WHERE a.id = ?
            """,
            (int(admin_id),),
        )
        return self._cursor_row_to_dict(cursor, cursor.fetchone())

    def _raise_nurse_optimistic_conflict(
        self,
        reason: str,
        message: str,
        *,
        admin_id: Optional[int] = None,
        row: Optional[dict] = None,
    ):
        row = row or {}
        resolved_admin_id = admin_id if admin_id is not None else row.get("id")
        admission_id = row.get("admission_id")
        logger.warning(
            "[OrdersSync] nurse_optimistic_conflict reason=%s admin_id=%s admission_id=%s order_id=%s "
            "planned_time=%s admin_status=%s order_status=%s version=%s latest_id=%s",
            reason,
            resolved_admin_id,
            admission_id,
            row.get("order_id"),
            row.get("planned_time"),
            row.get("status"),
            row.get("order_status"),
            row.get("version"),
            row.get("latest_id"),
        )
        try:
            admission_id_int = int(admission_id or 0)
        except Exception:
            admission_id_int = None
        record_orders_sync_event(
            "conflict",
            role="nurse",
            admission_id=admission_id_int,
            reason=reason,
            immediate=True,
        )
        raise RuntimeError(message)

    def _assert_nurse_admin_current(self, cursor, row: Optional[dict]) -> int:
        if not row:
            self._raise_nurse_optimistic_conflict(
                "admin_missing",
                "Назначение уже изменено или удалено. Обновите карточку.",
            )

        admin_status = str(row.get("status") or "")
        order_status = str(row.get("order_status") or "")
        if admin_status in ("deleted", "cancelled") or order_status in ("deleted", "cancelled"):
            self._raise_nurse_optimistic_conflict(
                "admin_or_order_not_active",
                "Назначение уже отменено или удалено. Обновите карточку.",
                row=row,
            )

        if int(row.get("is_committed") or 0) != 1:
            self._raise_nurse_optimistic_conflict(
                "admin_not_committed",
                "Назначение еще не сохранено врачом. Обновите карточку.",
                row=row,
            )

        cursor.execute(
            """
            SELECT id, version, status, is_committed
            FROM administrations
            WHERE order_id = ? AND planned_time = ?
              AND is_committed = 1
            ORDER BY id DESC
            LIMIT 1
            """,
            (row["order_id"], row["planned_time"]),
        )
        latest = self._cursor_row_to_dict(cursor, cursor.fetchone())
        if not latest or int(latest.get("id") or 0) != int(row.get("id") or 0):
            conflict_row = dict(row)
            if latest:
                conflict_row["latest_id"] = latest.get("id")
            self._raise_nurse_optimistic_conflict(
                "latest_admin_changed",
                "Назначение изменено врачом. Обновите карточку перед отметкой.",
                row=conflict_row,
            )

        latest_status = str(latest.get("status") or "")
        if latest_status in ("deleted", "cancelled"):
            conflict_row = dict(row)
            conflict_row["latest_id"] = latest.get("id")
            conflict_row["status"] = latest_status
            self._raise_nurse_optimistic_conflict(
                "latest_admin_not_active",
                "Назначение уже отменено или удалено. Обновите карточку.",
                row=conflict_row,
            )

        return int(row.get("version") or 0)

    @staticmethod
    def _safe_float(value, default: float = 0.0) -> float:
        try:
            if value is None or value == "":
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _normalize_volume_unit(value) -> str:
        try:
            from rem_card.services.balance_calculator import BalanceCalculator

            return BalanceCalculator._normalize_unit_token(str(value or ""))
        except Exception:
            return str(value or "").lower().strip()

    @staticmethod
    def _parse_volume_from_text(value) -> float:
        try:
            from rem_card.services.balance_calculator import BalanceCalculator

            return BalanceCalculator.parse_volume(str(value or ""))
        except Exception:
            return 0.0

    @staticmethod
    def _normalize_transfusion_datetime(value) -> str:
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        text = str(value or "").strip()
        if not text:
            return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            return datetime.fromisoformat(text.replace(" ", "T")).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return text.replace("T", " ").split(".")[0]

    @classmethod
    def _transfusion_volume_ml(cls, row: dict) -> int:
        volume = cls._safe_float(row.get("admin_volume_ml"), default=0.0)
        if volume <= 0:
            volume = cls._safe_float(row.get("dose_value"), default=0.0)
            unit = cls._normalize_volume_unit(row.get("dose_unit"))
            if unit in ("l", "л"):
                volume *= 1000.0

        if volume <= 0:
            for key in ("order_comment", "text", "latin"):
                volume = cls._parse_volume_from_text(row.get(key))
                if volume > 0:
                    break

        return max(0, int(round(volume)))

    def _fetch_admin_transfusion_row(self, cursor, admin_id: int) -> Optional[dict]:
        cursor.execute(
            """
            SELECT
                a.id AS admin_id,
                a.order_id,
                a.planned_time,
                a.actual_time,
                a.status AS admin_status,
                a.comment AS admin_comment,
                a.volume_ml AS admin_volume_ml,
                a.is_committed AS admin_is_committed,
                o.admission_id,
                o.drug_key,
                o.latin,
                o.text,
                o.dose_value,
                o.dose_unit,
                o.comment AS order_comment,
                o.status AS order_status
            FROM administrations a
            JOIN orders o ON o.id = a.order_id
            WHERE a.id = ?
            """,
            (admin_id,),
        )
        return self._cursor_row_to_dict(cursor, cursor.fetchone())

    @classmethod
    def _is_active_executed_transfusion_row(cls, row: dict) -> bool:
        drug_key = str(row.get("drug_key") or "").lower().strip()
        if drug_key not in TRANSFUSION_DRUG_KEYS:
            return False
        if str(row.get("admin_comment") or "") != NURSE_MARK_EXECUTED:
            return False
        if str(row.get("admin_status") or "") in ("deleted", "cancelled"):
            return False
        if str(row.get("order_status") or "") in ("deleted", "cancelled"):
            return False
        return int(row.get("admin_is_committed") or 0) == 1

    def _insert_transfusion_from_admin_row(self, cursor, row: dict):
        if not self._is_active_executed_transfusion_row(row):
            return

        volume_ml = self._transfusion_volume_ml(row)
        if volume_ml <= 0:
            logger.warning(
                "[TransfusionSync] Skipping %s admin_id=%s: empty transfusion volume",
                row.get("drug_key"),
                row.get("admin_id"),
            )
            return

        transfusion_time = self._normalize_transfusion_datetime(row.get("actual_time") or row.get("planned_time"))
        cursor.execute(
            """
            INSERT INTO transfusions (
                admission_id, type, volume_ml, datetime,
                source, source_order_id, source_admin_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.get("admission_id"),
                str(row.get("drug_key") or "").lower().strip(),
                volume_ml,
                transfusion_time,
                REMCARD_TRANSFUSION_SOURCE,
                row.get("order_id"),
                row.get("admin_id"),
            ),
        )

    def _sync_transfusion_for_admin(self, cursor, admin_id: int):
        cursor.execute(
            """
            DELETE FROM transfusions
            WHERE source = ? AND source_admin_id = ?
            """,
            (REMCARD_TRANSFUSION_SOURCE, admin_id),
        )

        row = self._fetch_admin_transfusion_row(cursor, admin_id)
        if row:
            self._insert_transfusion_from_admin_row(cursor, row)

    def sync_transfusions_for_admission(self, cursor, admission_id: int):
        cursor.execute(
            """
            DELETE FROM transfusions
            WHERE source = ? AND admission_id = ?
            """,
            (REMCARD_TRANSFUSION_SOURCE, admission_id),
        )
        cursor.execute(
            """
            SELECT
                a.id AS admin_id,
                a.order_id,
                a.planned_time,
                a.actual_time,
                a.status AS admin_status,
                a.comment AS admin_comment,
                a.volume_ml AS admin_volume_ml,
                a.is_committed AS admin_is_committed,
                o.admission_id,
                o.drug_key,
                o.latin,
                o.text,
                o.dose_value,
                o.dose_unit,
                o.comment AS order_comment,
                o.status AS order_status
            FROM administrations a
            JOIN orders o ON o.id = a.order_id
            WHERE o.admission_id = ?
              AND LOWER(COALESCE(o.drug_key, '')) IN ('blood', 'plasma')
              AND a.id IN (
                  SELECT MAX(a2.id)
                  FROM administrations a2
                  JOIN orders o2 ON o2.id = a2.order_id
                  WHERE o2.admission_id = ?
                    AND a2.is_committed = 1
                  GROUP BY a2.order_id, a2.planned_time
              )
            """,
            (admission_id, admission_id),
        )
        rows = cursor.fetchall()
        columns = [desc[0] for desc in (cursor.description or [])]
        for row in rows:
            mapped = dict(row) if hasattr(row, "keys") else {columns[idx]: row[idx] for idx in range(len(columns))}
            if mapped:
                self._insert_transfusion_from_admin_row(cursor, mapped)

    def set_nurse_status(self, admin_id: int, mark: str, performer_id: Optional[int] = None):
        """Устанавливает отметку выполнения медсестрой в поле comment."""
        self._sanitize_legacy_statuses_once()
        with self.db.remcard_transaction() as cursor:
            row = self._fetch_admin_for_nurse_update(cursor, admin_id)
            expected_version = self._assert_nurse_admin_current(cursor, row)
            now_str = datetime.now().isoformat()

            color = None
            if mark == NURSE_MARK_EXECUTED:
                color = "#e8f5e9"
            elif mark == NURSE_MARK_NOT_EXECUTED:
                color = "#ffebee"

            color_until = (datetime.now() + timedelta(milliseconds=1200)).isoformat() if color else None

            query = """
                UPDATE administrations
                SET comment = ?,
                    actual_time = ?,
                    performer_id = ?,
                    ui_color = ?,
                    ui_color_until = ?,
                    version = COALESCE(version, 0) + 1,
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ? AND COALESCE(version, 0) = ?
            """
            cursor.execute(query, (mark, now_str, performer_id, color, color_until, admin_id, expected_version))
            if cursor.rowcount != 1:
                self._raise_nurse_optimistic_conflict(
                    "version_update_mismatch",
                    "Назначение изменено другим рабочим местом. Обновите карточку.",
                    admin_id=admin_id,
                    row=row,
                )
            self._sync_transfusion_for_admin(cursor, admin_id)
            logger.info(f"[NurseAction] Admin ID {admin_id} mark set to {mark} at {now_str}.")

    def cancel_nurse_action(self, admin_id: int, *, allow_late_cancel: bool = False):
        """Отменяет действие медсестры (очищает comment и actual_time)."""
        with self.db.remcard_transaction() as cursor:
            row = self._fetch_admin_for_nurse_update(cursor, admin_id)
            expected_version = self._assert_nurse_admin_current(cursor, row)

            if row["actual_time"] and not allow_late_cancel:
                act_time = datetime.fromisoformat(row["actual_time"])
                if datetime.now() > act_time + timedelta(minutes=60):
                    logger.warning(f"[NurseAction] Cancel timeout (60m) expired for Admin ID {admin_id}")
                    return

            query = """
                UPDATE administrations
                SET comment = '',
                    actual_time = NULL,
                    performer_id = NULL,
                    ui_color = NULL,
                    ui_color_until = NULL,
                    version = COALESCE(version, 0) + 1,
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ? AND COALESCE(version, 0) = ?
            """
            cursor.execute(query, (admin_id, expected_version))
            if cursor.rowcount != 1:
                self._raise_nurse_optimistic_conflict(
                    "version_cancel_mismatch",
                    "Назначение изменено другим рабочим местом. Обновите карточку.",
                    admin_id=admin_id,
                    row=row,
                )
            self._sync_transfusion_for_admin(cursor, admin_id)
            logger.info(f"[NurseAction] Admin ID {admin_id} mark cleared.")

    def set_doctor_status(self, admin_id: int, mark: str, performer_id: Optional[int] = None):
        self.set_nurse_status(admin_id, mark, performer_id=performer_id)

    def cancel_doctor_action(self, admin_id: int):
        self.cancel_nurse_action(admin_id, allow_late_cancel=True)

    def get_nurse_orders_data(self, admission_id: int, shift_date: datetime) -> List[Dict]:
        """Получает данные для сектора 1а. Просроченность больше не создается."""
        # В read-поллинге не запускаем write-maintenance, чтобы не блокировать UI.
        self._sanitize_legacy_statuses_once(allow_write=False)

        start_dt, end_dt = self._get_day_period_local(shift_date)

        query = """
            SELECT a.*, o.latin, o.drug_key, o.dose_value, o.dose_unit, o.comment as order_comment,
                   o.type as order_type
            FROM administrations a
            JOIN orders o ON a.order_id = o.id
            WHERE o.admission_id = ?
              AND a.planned_time >= ? AND a.planned_time < ?
              AND a.is_committed = 1
              AND a.id IN (
                  SELECT MAX(a2.id)
                  FROM administrations a2
                  WHERE a2.is_committed = 1
                  GROUP BY a2.order_id, a2.planned_time
              )
              AND a.cell_role IN ('start', 'single')
              AND COALESCE(a.status, '') = 'planned'
              /* Убрано AND COALESCE(o.status, '') != 'deleted', чтобы сектора 1а/5
                 не реагировали на черновики удаления до нажатия Сохранить */
        """
        rows = self.db.fetch_all_remcard(query, (admission_id, start_dt.isoformat(), end_dt.isoformat()))

        if rows:
            logger.debug(f"[OrderDomainService] get_nurse_orders_data: Found {len(rows)} committed admin rows for Admission {admission_id}")

        groups_dict = self._load_groups_priority()
        drugs_dict = self._load_drugs_groups()

        result = []

        for r in rows:
            rd = dict(r)

            drug_key = rd["drug_key"]
            group_key = drugs_dict.get(drug_key, "unknown")
            priority = groups_dict.get(group_key, 999)

            rd["priority"] = priority
            rd["group_name"] = group_key
            result.append(rd)

        return result

    def _get_day_period_local(self, date: datetime) -> Tuple[datetime, datetime]:
        start = date.replace(hour=8, minute=0, second=0, microsecond=0)
        if date.hour < 8:
            start -= timedelta(days=1)
        return start, start + timedelta(days=1)

    @classmethod
    def _load_seed_json_cached(cls, file_name: str) -> Dict:
        from rem_card.app.paths import SEED_DIR

        path = os.path.join(SEED_DIR, file_name)
        try:
            mtime = float(os.path.getmtime(path))
        except Exception:
            return {}

        with cls._seed_cache_lock:
            cached = cls._seed_cache.get(path)
            if cached and cached[0] == mtime:
                return dict(cached[1])

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
        except Exception:
            return {}

        with cls._seed_cache_lock:
            cls._seed_cache[path] = (mtime, data)
        return dict(data)

    def _load_groups_priority(self) -> Dict[str, int]:
        data = self._load_seed_json_cached("groups.seed.json")
        return {k: v.get("priority_level", 99) for k, v in data.items() if isinstance(v, dict)}

    def _load_drugs_groups(self) -> Dict[str, str]:
        data = self._load_seed_json_cached("drugs.seed.json")
        return {k: v.get("group", "unknown") for k, v in data.items() if isinstance(v, dict)}

    def _create_chain(self, cursor, order: OrderDTO, start_time: datetime):
        # Логика границы карты
        limit_time = start_time.replace(hour=8, minute=0, second=0, microsecond=0)
        if start_time.hour >= 8:
            limit_time += timedelta(days=1)

        duration = order.duration_min if order.duration_min else 0
        num_desired = int((limit_time - start_time).total_seconds() / 3600) if duration == -1 else (duration - 1) // 60 + 1

        desired_slots = []
        for i in range(num_desired):
            cell_time = start_time + timedelta(hours=i)
            if cell_time >= limit_time:
                break
            desired_slots.append(cell_time)

        last_rows_by_time = self._get_last_admins_for_times(cursor, order.id, desired_slots)
        available_slots = []
        for i, cell_time in enumerate(desired_slots):
            last_row = last_rows_by_time.get(cell_time.isoformat())
            if last_row and self._is_planned_row(last_row) and i > 0:
                break
            available_slots.append(cell_time)

        if not available_slots:
            return

        num_actual = len(available_slots)
        big_chain_id = str(uuid.uuid4()) if num_actual > 1 else None

        for i, cell_time in enumerate(available_slots):
            role = "single" if num_actual == 1 else ("start" if i == 0 else ("end" if i == num_actual - 1 else "body"))
            self._insert_draft(
                cursor,
                order.id,
                cell_time,
                "planned",
                role,
                big_chain_id,
                last_row=last_rows_by_time.get(cell_time.isoformat()),
            )




