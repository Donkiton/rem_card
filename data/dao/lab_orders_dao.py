from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from rem_card.data.dto.lab_orders_dto import LabOrderDTO, LabOrderStatus


class LabOrdersDAO:
    def __init__(self, db_manager):
        self.db = db_manager

    @staticmethod
    def _parse_dt(value: Any) -> Optional[datetime]:
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            return value
        text = str(value).strip()
        if not text:
            return None
        candidates = (text, text.replace(" ", "T"))
        for candidate in candidates:
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

    @staticmethod
    def _dt_value(value: Optional[datetime]) -> Optional[str]:
        return value.isoformat(timespec="seconds") if value else None

    @staticmethod
    def _row_dict(row) -> dict[str, Any]:
        return dict(row) if row is not None else {}

    def get_admission_patient_id(self, admission_id: int) -> Optional[int]:
        row = self.db.fetch_one_remcard(
            "SELECT patient_id FROM admissions WHERE id = ?",
            (int(admission_id),),
        )
        if not row:
            return None
        data = self._row_dict(row)
        patient_id = data.get("patient_id")
        return int(patient_id) if patient_id is not None else None

    def list_for_card_day(
        self,
        admission_id: int,
        *,
        card_day_id: Optional[str],
        start_dt: Optional[datetime],
        end_dt: Optional[datetime],
    ) -> list[LabOrderDTO]:
        params: list[Any] = [int(admission_id)]
        day_filters: list[str] = []
        if card_day_id:
            day_filters.append("card_day_id = ?")
            params.append(str(card_day_id))
        if start_dt is not None and end_dt is not None:
            start_value = self._dt_value(start_dt)
            end_value = self._dt_value(end_dt)
            day_filters.append(
                """
                (
                    card_day_id IS NULL
                    AND (
                        (scheduled_at IS NOT NULL AND DATETIME(scheduled_at) >= DATETIME(?) AND DATETIME(scheduled_at) < DATETIME(?))
                        OR (created_at IS NOT NULL AND DATETIME(created_at) >= DATETIME(?) AND DATETIME(created_at) < DATETIME(?))
                        OR (completed_at IS NOT NULL AND DATETIME(completed_at) >= DATETIME(?) AND DATETIME(completed_at) < DATETIME(?))
                    )
                )
                """
            )
            params.extend([start_value, end_value, start_value, end_value, start_value, end_value])

        where = "admission_id = ?"
        if day_filters:
            where += " AND (" + " OR ".join(day_filters) + ")"

        rows = self.db.fetch_all_remcard(
            f"""
            SELECT *
            FROM lab_orders
            WHERE {where}
            ORDER BY DATETIME(created_at) DESC, id DESC
            """,
            tuple(params),
        )
        return [self._map(row) for row in rows]

    def save_lab_order(self, cursor, dto: LabOrderDTO) -> int:
        if dto.id is None:
            cursor.execute(
                """
                INSERT INTO lab_orders (
                    patient_id, admission_id, card_day_id,
                    analysis_code, analysis_name, material, status,
                    created_at, scheduled_at, completed_at, comment,
                    created_by_role, created_by_user,
                    completed_by_role, completed_by_user, revision
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dto.patient_id,
                    int(dto.admission_id),
                    dto.card_day_id,
                    dto.analysis_code,
                    dto.analysis_name,
                    dto.material,
                    dto.status,
                    self._dt_value(dto.created_at),
                    self._dt_value(dto.scheduled_at),
                    self._dt_value(dto.completed_at),
                    dto.comment,
                    dto.created_by_role,
                    dto.created_by_user,
                    dto.completed_by_role,
                    dto.completed_by_user,
                    int(dto.revision or 0),
                ),
            )
            dto.id = int(cursor.lastrowid)
            return int(dto.id)

        cursor.execute(
            """
            UPDATE lab_orders
            SET analysis_code = ?,
                analysis_name = ?,
                material = ?,
                status = ?,
                scheduled_at = ?,
                completed_at = ?,
                comment = ?,
                completed_by_role = ?,
                completed_by_user = ?,
                revision = COALESCE(revision, 0) + 1,
                updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
            WHERE id = ?
            """,
            (
                dto.analysis_code,
                dto.analysis_name,
                dto.material,
                dto.status,
                self._dt_value(dto.scheduled_at),
                self._dt_value(dto.completed_at),
                dto.comment,
                dto.completed_by_role,
                dto.completed_by_user,
                int(dto.id),
            ),
        )
        return int(dto.id)

    def mark_completed(
        self,
        cursor,
        order_id: int,
        *,
        completed_at: datetime,
        completed_by_role: str = "nurse",
        completed_by_user: Optional[str] = None,
    ) -> None:
        cursor.execute(
            """
            UPDATE lab_orders
            SET status = ?,
                completed_at = ?,
                completed_by_role = ?,
                completed_by_user = ?,
                revision = COALESCE(revision, 0) + 1,
                updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
            WHERE id = ?
            """,
            (
                LabOrderStatus.COMPLETED.value,
                self._dt_value(completed_at),
                completed_by_role,
                completed_by_user,
                int(order_id),
            ),
        )
        if cursor.rowcount == 0:
            raise ValueError("Назначение анализа не найдено.")

    def delete_for_admission(self, cursor, admission_id: int) -> None:
        cursor.execute("DELETE FROM lab_orders WHERE admission_id = ?", (int(admission_id),))

    def delete_for_card_day(
        self,
        cursor,
        admission_id: int,
        *,
        card_day_id: Optional[str],
        start_dt: Optional[datetime],
        end_dt: Optional[datetime],
    ) -> None:
        params: list[Any] = [int(admission_id)]
        filters: list[str] = []
        if card_day_id:
            filters.append("card_day_id = ?")
            params.append(str(card_day_id))
        if start_dt is not None and end_dt is not None:
            start_value = self._dt_value(start_dt)
            end_value = self._dt_value(end_dt)
            filters.append(
                """
                (
                    card_day_id IS NULL
                    AND (
                        (scheduled_at IS NOT NULL AND DATETIME(scheduled_at) >= DATETIME(?) AND DATETIME(scheduled_at) < DATETIME(?))
                        OR (created_at IS NOT NULL AND DATETIME(created_at) >= DATETIME(?) AND DATETIME(created_at) < DATETIME(?))
                    )
                )
                """
            )
            params.extend([start_value, end_value, start_value, end_value])
        if not filters:
            return
        cursor.execute(
            f"DELETE FROM lab_orders WHERE admission_id = ? AND ({' OR '.join(filters)})",
            tuple(params),
        )

    def _map(self, row) -> LabOrderDTO:
        data = self._row_dict(row)
        return LabOrderDTO(
            id=data.get("id"),
            patient_id=data.get("patient_id"),
            admission_id=int(data.get("admission_id") or 0),
            card_day_id=data.get("card_day_id"),
            analysis_code=data.get("analysis_code") or "",
            analysis_name=data.get("analysis_name") or "",
            material=data.get("material") or "",
            status=data.get("status") or LabOrderStatus.ASSIGNED.value,
            created_at=self._parse_dt(data.get("created_at")),
            scheduled_at=self._parse_dt(data.get("scheduled_at")),
            completed_at=self._parse_dt(data.get("completed_at")),
            comment=data.get("comment") or "",
            created_by_role=data.get("created_by_role") or "doctor",
            created_by_user=data.get("created_by_user"),
            completed_by_role=data.get("completed_by_role"),
            completed_by_user=data.get("completed_by_user"),
            revision=int(data.get("revision") or 0),
            created_at_db=self._parse_dt(data.get("created_at_db")),
            updated_at=self._parse_dt(data.get("updated_at")),
        )
