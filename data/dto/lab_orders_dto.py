from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class LabOrderStatus(Enum):
    ASSIGNED = "assigned"
    COMPLETED = "completed"


class LabMaterial(Enum):
    VENOUS_BLOOD = "venous_blood"
    ARTERIAL_BLOOD = "arterial_blood"
    URINE = "urine"
    LIQUOR = "liquor"


LAB_ORDER_STATUS_LABELS = {
    LabOrderStatus.ASSIGNED.value: "Назначено",
    LabOrderStatus.COMPLETED.value: "Выполнено",
}

LAB_MATERIAL_LABELS = {
    LabMaterial.VENOUS_BLOOD.value: "Кровь венозная",
    LabMaterial.ARTERIAL_BLOOD.value: "Кровь артериальная",
    LabMaterial.URINE.value: "Моча",
    LabMaterial.LIQUOR.value: "Ликвор",
}


@dataclass
class LabOrderDTO:
    id: Optional[int] = None
    patient_id: Optional[int] = None
    admission_id: int = 0
    card_day_id: Optional[str] = None
    analysis_code: str = ""
    analysis_name: str = ""
    material: str = LabMaterial.VENOUS_BLOOD.value
    status: str = LabOrderStatus.ASSIGNED.value
    created_at: Optional[datetime] = None
    scheduled_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    comment: str = ""
    created_by_role: str = "doctor"
    created_by_user: Optional[str] = None
    completed_by_role: Optional[str] = None
    completed_by_user: Optional[str] = None
    revision: int = 0
    created_at_db: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "patient_id": self.patient_id,
            "admission_id": self.admission_id,
            "card_day_id": self.card_day_id,
            "analysis_code": self.analysis_code,
            "analysis_name": self.analysis_name,
            "material": self.material,
            "material_label": LAB_MATERIAL_LABELS.get(self.material, self.material),
            "status": self.status,
            "status_label": LAB_ORDER_STATUS_LABELS.get(self.status, "Назначено"),
            "created_at": self.created_at,
            "scheduled_at": self.scheduled_at,
            "completed_at": self.completed_at,
            "comment": self.comment,
            "created_by_role": self.created_by_role,
            "created_by_user": self.created_by_user,
            "completed_by_role": self.completed_by_role,
            "completed_by_user": self.completed_by_user,
            "revision": self.revision,
            "created_at_db": self.created_at_db,
            "updated_at": self.updated_at,
        }
