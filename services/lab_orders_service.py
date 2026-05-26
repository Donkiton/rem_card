from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Callable, Optional, Sequence

from rem_card.data.dao.lab_orders_dao import LabOrdersDAO
from rem_card.data.dto.lab_orders_dto import (
    LAB_MATERIAL_LABELS,
    LabOrderDTO,
    LabOrderStatus,
)
from rem_card.services.lab_analysis_catalog_service import LabAnalysisCatalogService, normalize_lab_material


class LabOrdersService:
    def __init__(self, dao: LabOrdersDAO, data_service=None, catalog_service: Optional[LabAnalysisCatalogService] = None):
        self.dao = dao
        self.data_service = data_service
        self.catalog_service = catalog_service or LabAnalysisCatalogService()

    @staticmethod
    def card_day_id_from_shift_start(shift_start: datetime) -> str:
        return shift_start.isoformat(timespec="minutes").replace("T", " ")

    def list_orders_for_card_day(
        self,
        admission_id: int,
        *,
        card_day_id: Optional[str],
        start_dt: Optional[datetime],
        end_dt: Optional[datetime],
    ) -> list[LabOrderDTO]:
        return self.dao.list_for_card_day(
            int(admission_id),
            card_day_id=card_day_id,
            start_dt=start_dt,
            end_dt=end_dt,
        )

    def build_snapshot(
        self,
        admission_id: int,
        *,
        card_day_id: Optional[str],
        start_dt: Optional[datetime],
        end_dt: Optional[datetime],
    ) -> dict[str, Any]:
        orders = self.list_orders_for_card_day(
            int(admission_id),
            card_day_id=card_day_id,
            start_dt=start_dt,
            end_dt=end_dt,
        )
        material_labels = self._material_labels()
        rows = [self._row_payload(order, material_labels) for order in orders]
        assigned_count = sum(1 for row in rows if row.get("status") != LabOrderStatus.COMPLETED.value)
        completed_count = sum(1 for row in rows if row.get("status") == LabOrderStatus.COMPLETED.value)
        return {
            "scope": "lab_orders",
            "admission_id": int(admission_id),
            "card_day_id": card_day_id,
            "start_dt": start_dt,
            "end_dt": end_dt,
            "rows": rows,
            "counts": {
                "assigned": assigned_count,
                "completed": completed_count,
                "total": len(rows),
            },
            "content_hash": self._content_hash(rows),
        }

    def create_lab_order(
        self,
        *,
        admission_id: int,
        card_day_id: Optional[str],
        analysis_code: str,
        analysis_name: str,
        material: str,
        scheduled_at: datetime,
        created_at: Optional[datetime] = None,
        comment: str = "",
        created_by_role: str = "doctor",
        created_by_user: Optional[str] = None,
    ) -> int:
        patient_id = self.dao.get_admission_patient_id(int(admission_id))
        if patient_id is None:
            raise ValueError("Госпитализация пациента не найдена.")
        normalized_material = self._normalize_material(material)
        created_dt = created_at or datetime.now().replace(microsecond=0)
        dto = LabOrderDTO(
            patient_id=patient_id,
            admission_id=int(admission_id),
            card_day_id=card_day_id,
            analysis_code=str(analysis_code or "").strip(),
            analysis_name=str(analysis_name or "").strip(),
            material=normalized_material,
            status=LabOrderStatus.ASSIGNED.value,
            created_at=created_dt,
            scheduled_at=scheduled_at,
            comment=str(comment or "").strip(),
            created_by_role=created_by_role or "doctor",
            created_by_user=created_by_user,
        )
        if not dto.analysis_name:
            raise ValueError("Укажите анализ.")

        def operation(cursor):
            return self.dao.save_lab_order(cursor, dto)

        return int(self._run_write(f"lab_order_create:{admission_id}", operation))

    def create_lab_orders(
        self,
        *,
        admission_id: int,
        card_day_id: Optional[str],
        orders: Sequence[dict[str, Any]],
        created_by_role: str = "doctor",
        created_by_user: Optional[str] = None,
    ) -> list[int]:
        patient_id = self.dao.get_admission_patient_id(int(admission_id))
        if patient_id is None:
            raise ValueError("Госпитализация пациента не найдена.")
        now = datetime.now().replace(microsecond=0)
        dtos: list[LabOrderDTO] = []
        for raw in orders or []:
            analysis_name = str(raw.get("analysis_name") or raw.get("name") or "").strip()
            if not analysis_name:
                raise ValueError("Укажите анализ.")
            scheduled_at = self._parse_datetime(raw.get("scheduled_at"))
            if scheduled_at is None:
                raise ValueError(f"Не указано время для анализа «{analysis_name}».")
            dtos.append(
                LabOrderDTO(
                    patient_id=patient_id,
                    admission_id=int(admission_id),
                    card_day_id=card_day_id,
                    analysis_code=str(raw.get("analysis_code") or raw.get("code") or "").strip(),
                    analysis_name=analysis_name,
                    material=self._normalize_material(raw.get("material")),
                    status=LabOrderStatus.ASSIGNED.value,
                    created_at=now,
                    scheduled_at=scheduled_at,
                    comment=str(raw.get("comment") or "").strip(),
                    created_by_role=created_by_role or "doctor",
                    created_by_user=created_by_user,
                )
            )
        if not dtos:
            raise ValueError("Не выбраны анализы для назначения.")

        def operation(cursor):
            return [self.dao.save_lab_order(cursor, dto) for dto in dtos]

        return list(self._run_write(f"lab_orders_create:{admission_id}", operation))

    def list_analysis_templates(self) -> list[dict[str, Any]]:
        return self.catalog_service.list_templates()

    def mark_completed(
        self,
        order_id: int,
        *,
        completed_at: Optional[datetime] = None,
        completed_by_role: str = "nurse",
        completed_by_user: Optional[str] = None,
    ) -> int:
        completed_dt = completed_at or datetime.now().replace(microsecond=0)

        def operation(cursor):
            self.dao.mark_completed(
                cursor,
                int(order_id),
                completed_at=completed_dt,
                completed_by_role=completed_by_role or "nurse",
                completed_by_user=completed_by_user,
            )
            return int(order_id)

        return int(self._run_write(f"lab_order_complete:{order_id}", operation))

    def update_lab_order_details(
        self,
        order_id: int,
        *,
        material: str,
        scheduled_at: datetime,
        comment: str = "",
        expected_revision: Optional[int] = None,
    ) -> int:
        scheduled_dt = self._parse_datetime(scheduled_at)
        if scheduled_dt is None:
            raise ValueError("Укажите время назначения анализа.")
        normalized_material = self._normalize_material(material)

        def operation(cursor):
            self.dao.update_lab_order_details(
                cursor,
                int(order_id),
                material=normalized_material,
                scheduled_at=scheduled_dt,
                comment=str(comment or "").strip(),
                expected_revision=expected_revision,
            )
            return int(order_id)

        return int(self._run_write(f"lab_order_update:{order_id}", operation))

    def delete_lab_orders(self, admission_id: int, *, order_ids: Sequence[int]) -> int:
        normalized_ids = sorted({int(order_id) for order_id in (order_ids or []) if order_id})
        if not normalized_ids:
            raise ValueError("Не выбраны анализы для удаления.")

        def operation(cursor):
            return self.dao.delete_lab_orders(cursor, int(admission_id), normalized_ids)

        return int(self._run_write(f"lab_orders_delete:{admission_id}", operation))

    def _run_write(self, description: str, operation: Callable):
        if self.data_service:
            return self.data_service.run_write(description, operation)
        return self.dao.db.run_write_operation(operation, source=description)

    @staticmethod
    def _parse_datetime(value: Any) -> Optional[datetime]:
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
                pass
        return None

    def _row_payload(self, order: LabOrderDTO, material_labels: Optional[dict[str, str]] = None) -> dict[str, Any]:
        row = order.as_dict()
        material_labels = material_labels or self._material_labels()
        material = self._normalize_material(row.get("material"), material_labels)
        row["material"] = material
        row["material_label"] = material_labels.get(material, row.get("material_label") or material)
        row["status"] = (
            LabOrderStatus.COMPLETED.value
            if str(row.get("status") or "").lower() == LabOrderStatus.COMPLETED.value
            else LabOrderStatus.ASSIGNED.value
        )
        return row

    @staticmethod
    def _content_hash(rows: Sequence[dict[str, Any]]) -> str:
        stable_rows: list[dict[str, Any]] = []
        for row in rows or []:
            stable_rows.append(
                {
                    "id": row.get("id"),
                    "patient_id": row.get("patient_id"),
                    "admission_id": row.get("admission_id"),
                    "card_day_id": row.get("card_day_id"),
                    "analysis_code": row.get("analysis_code"),
                    "analysis_name": row.get("analysis_name"),
                    "material": row.get("material"),
                    "status": row.get("status"),
                    "created_at": row.get("created_at"),
                    "scheduled_at": row.get("scheduled_at"),
                    "completed_at": row.get("completed_at"),
                    "comment": row.get("comment"),
                    "revision": row.get("revision"),
                    "updated_at": row.get("updated_at"),
                }
            )
        payload = json.dumps(stable_rows, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _material_labels(self) -> dict[str, str]:
        try:
            return self.catalog_service.material_labels()
        except Exception:
            return dict(LAB_MATERIAL_LABELS)

    def _normalize_material(self, value: Any, material_labels: Optional[dict[str, str]] = None) -> str:
        labels = material_labels if material_labels is not None else self._material_labels()
        return normalize_lab_material(value, labels)
