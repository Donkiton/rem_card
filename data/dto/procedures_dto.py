from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class ProcedureType(Enum):
    CVC = "CVC"
    LUMBAR_PUNCTURE = "LUMBAR_PUNCTURE"
    TRANSFUSION = "TRANSFUSION"


class ProcedureStatus(Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    CATHETER_REMOVED = "catheter_removed"
    CATHETER_REPLACED = "catheter_replaced"


class ConsentKind(Enum):
    CVC_CONSENT = "CVC_CONSENT"
    LUMBAR_PUNCTURE_CONSENT = "LUMBAR_PUNCTURE_CONSENT"
    TRANSFUSION_CONSENT = "TRANSFUSION_CONSENT"


class ConsentMode(Enum):
    PATIENT = "patient"
    REPRESENTATIVE = "representative"
    CONSILIUM = "consilium"
    EMERGENCY_DOCTOR_DECISION = "emergency_doctor_decision"


PROCEDURE_TYPE_LABELS = {
    ProcedureType.CVC.value: "ЦВК",
    ProcedureType.LUMBAR_PUNCTURE.value: "Люмбальная пункция",
    ProcedureType.TRANSFUSION.value: "Гемотрансфузия",
}

PROCEDURE_STATUS_LABELS = {
    ProcedureStatus.DRAFT.value: "Черновик",
    ProcedureStatus.ACTIVE.value: "Активна",
    ProcedureStatus.COMPLETED.value: "Завершена",
    ProcedureStatus.CANCELLED.value: "Отменена",
    ProcedureStatus.CATHETER_REMOVED.value: "Удалён катетер",
    ProcedureStatus.CATHETER_REPLACED.value: "Переустановлен катетер",
}


@dataclass
class ProcedureDTO:
    id: Optional[int] = None
    patient_id: Optional[int] = None
    admission_id: int = 0
    procedure_type: str = ProcedureType.CVC.value
    status: str = ProcedureStatus.DRAFT.value
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration_minutes: Optional[int] = None
    doctor_id: Optional[int] = None
    doctor_name_snapshot: str = ""
    department_snapshot: str = ""
    patient_snapshot_json: str = "{}"
    diagnosis_snapshot: str = ""
    notes: str = ""
    created_by: str = "doctor"
    updated_by: str = "doctor"
    revision: int = 0
    is_deleted: int = 0
    procedure_subtype: str = ""


@dataclass
class ProcedureConsentDTO:
    id: Optional[int] = None
    procedure_id: int = 0
    consent_kind: str = ConsentKind.CVC_CONSENT.value
    consent_mode: str = ConsentMode.PATIENT.value
    patient_signed: int = 1
    representative_name: str = ""
    representative_details: str = ""
    diagnosis_snapshot: str = ""
    doctor_name_snapshot: str = ""
    consilium_json: str = "{}"
    emergency_reason: str = ""
    created_at: Optional[datetime] = None
    printed_at: Optional[datetime] = None
    revision: int = 0


@dataclass
class ProcedureCvcDTO:
    procedure_id: int = 0
    cvc_code_main_selected: int = 1
    cvc_code_tunneled_selected: int = 0
    indications: list[str] = field(default_factory=list)
    indications_other: str = ""
    procedure_place_code: str = ""
    procedure_place_other: str = ""
    anesthesia_code: str = ""
    anesthesia_other: str = ""
    access_code: str = ""
    access_other: str = ""
    method_code: str = ""
    method_other: str = ""
    ultrasound_control: list[str] = field(default_factory=list)
    attempts_count: Optional[int] = None
    diameter_f: Optional[float] = None
    length_cm: Optional[float] = None
    lumens_count: Optional[int] = None
    fixation: list[str] = field(default_factory=list)
    fixation_other: str = ""
    position_confirmed_at: Optional[datetime] = None
    position_confirmation: list[str] = field(default_factory=list)
    position_confirmation_comment: str = ""
    technical_difficulty_code: str = "none"
    technical_difficulty_description: str = ""
    actions_taken: str = ""
    catheter_status: str = "active"
    removed_or_replaced: str = ""
    removed_at: Optional[datetime] = None
    usage_complications_code: str = "none"
    usage_complications_description: str = ""
    additional_treatment: str = ""
    operator_doctor_name: str = ""
    removal_doctor_name: str = ""
    revision: int = 0


@dataclass
class ProcedureLumbarPunctureDTO:
    procedure_id: int = 0
    indications: list[str] = field(default_factory=list)
    indications_other: str = ""
    procedure_place_code: str = ""
    procedure_place_other: str = ""
    anesthesia_code: str = ""
    anesthesia_other: str = ""
    access_code: str = ""
    access_other: str = ""
    level_code: str = ""
    level_other: str = ""
    technical_difficulty_code: str = "none"
    technical_difficulty_description: str = ""
    actions_taken: str = ""
    result_code: str = "csf_obtained"
    csf_characteristics: str = ""
    result_notes: str = ""
    operator_doctor_name: str = ""
    revision: int = 0


@dataclass
class ProcedureTransfusionDTO:
    procedure_id: int = 0
    request_at: Optional[datetime] = None
    indication_code: str = ""
    recipient_abo: str = ""
    recipient_rh: str = ""
    recipient_antigens: str = ""
    alloimmune_antibodies: str = "negative"
    transfusions_history: str = "no"
    reactions_history: str = "no"
    reactions_history_details: str = ""
    individual_selection_history: str = "no"
    donor_component_name: str = ""
    procurement_org: str = "КГБУЗ 'КСПК', г.Комсомолькс-на-Амуре ."
    donor_abo: str = ""
    donor_rh: str = ""
    donor_antigens: str = ""
    donor_code: str = ""
    unit_number: str = ""
    volume_ml: Optional[int] = None
    collection_date: str = ""
    expiration_date: str = ""
    selection_medical_org: str = ""
    selection_study_date: str = ""
    selection_responsible_name: str = ""
    selection_conclusion: str = ""
    reagent_anti_a_series: str = "069F"
    reagent_anti_a_expiration: str = ""
    reagent_anti_b_series: str = "070R"
    reagent_anti_b_expiration: str = ""
    reagent_anti_d_series: str = "080"
    reagent_anti_d_expiration: str = ""
    plane_compatibility: str = "совместимо"
    biological_test: str = "совместимо"
    reaction_symptoms: str = ""
    reaction_severity: str = ""
    observation_json: str = "{}"
    operator_doctor_name: str = ""
    revision: int = 0


@dataclass
class ProcedureBundle:
    procedure: ProcedureDTO
    cvc: Optional[ProcedureCvcDTO] = None
    lumbar_puncture: Optional[ProcedureLumbarPunctureDTO] = None
    transfusion: Optional[ProcedureTransfusionDTO] = None
    consent: Optional[ProcedureConsentDTO] = None
    patient_snapshot: dict[str, Any] = field(default_factory=dict)
