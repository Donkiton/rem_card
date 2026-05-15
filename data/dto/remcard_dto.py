from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional, List, Dict, Any
from enum import Enum

from rem_card.app.patient_age import format_patient_age, format_patient_age_from_birth_date

class OrderType(Enum):
    MEDICATION = "medication"
    INFUSION_CONTINUOUS = "infusion_cont"
    INFUSION_INTERMITTENT = "infusion_inter"
    PROCEDURE = "procedure"
    OBSERVATION = "observation"

class OrderStatus(Enum):
    ACTIVE = "active"
    HELD = "held"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    DELETED = "deleted"

class PatientStatus(Enum):
    ACTIVE = "ACTIVE"           # В отделении
    OUT = "OUT"                 # Вне отделения (КТ, перевязка и т.д.)
    OR = "OR"                   # Операционная
    TRANSFERRED = "TRANSFERRED" # Переведен (ИСХОД)
    DEAD = "DEAD"               # Умер (ИСХОД)

    def is_outcome(self) -> bool:
        """Объединяет финальные статусы (Переведен/Умер) под понятием 'ИСХОД'."""
        return self in (PatientStatus.TRANSFERRED, PatientStatus.DEAD)


class VentilationStartType(Enum):
    ADMISSION = "ADMISSION"
    IN_DEPARTMENT = "IN_DEPARTMENT"


class VentilationDeliveryType(Enum):
    SELF = "SELF"
    AMBU = "AMBU"
    APPARATUS = "APPARATUS"
    UNKNOWN = "UNKNOWN"


class VentilationEventType(Enum):
    START_VENT = "START_VENT"
    MODE_CHANGE = "MODE_CHANGE"
    EXTUBATION = "EXTUBATION"
    TRACHEOSTOMY = "TRACHEOSTOMY"
    TUBE_REPLACEMENT = "TUBE_REPLACEMENT"


class VentilationMode(Enum):
    CONTROLLED_VCV = "CONTROLLED_VCV"
    CONTROLLED_PCV = "CONTROLLED_PCV"
    SIMV_VC = "SIMV_VC"
    SIMV_PC = "SIMV_PC"
    PSV = "PSV"
    CPAP = "CPAP"
    BIPAP = "BIPAP"
    SPONTANEOUS = "SPONTANEOUS"

@dataclass
class PatientStatusEventDTO:
    id: Optional[int] = None
    admission_id: int = 0
    status: PatientStatus = PatientStatus.ACTIVE
    reason_type: Optional[str] = None
    reason_text: Optional[str] = None
    start_time: datetime = field(default_factory=datetime.now)
    end_time: Optional[datetime] = None
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    revision: int = 0

@dataclass
class PatientDTO:
    id: int  # Это admission_id
    last_name: Optional[str]
    first_name: Optional[str]
    middle_name: Optional[str]
    history_number: str
    bed_number: Optional[int] = None
    admission_uid: Optional[str] = None
    admission_datetime: Optional[datetime] = None
    transfer_datetime: Optional[datetime] = None
    diagnosis_text: Optional[str] = None
    
    # Новые поля для расширенной синхронизации
    age: Optional[int] = None
    age_months: Optional[int] = None
    age_unit: Optional[str] = "л"
    birth_date: Optional[date] = None
    mkb_code: Optional[str] = None
    operation_info: Optional[str] = None
    emergency_notice_number: Optional[str] = None
    emergency_notice_entered_at: Optional[datetime] = None
    full_name: Optional[str] = None
    source_db_path: Optional[str] = None
    source_db_name: Optional[str] = None
    source_admission_id: Optional[int] = None
    is_external_archive: bool = False

    def get_display_name(self) -> str:
        if not self.last_name and not self.first_name and not self.middle_name:
            return self.full_name if self.full_name else "Неизвестно"
            
        last = self.last_name if self.last_name else ""
        f = self.first_name if self.first_name else ""
        m = self.middle_name if self.middle_name else ""
        
        # Убираем лишние пробелы, если какие-то части имени отсутствуют
        name = " ".join(part for part in [last, f, m] if part).strip()
        return name if name else (self.full_name if self.full_name else "Неизвестно")

    def get_display_age(self, reference_date: Optional[datetime] = None) -> str:
        age_text = format_patient_age_from_birth_date(
            self.birth_date,
            reference_date or self.admission_datetime,
        )
        if age_text:
            return age_text
        return format_patient_age(self.age, self.age_unit, self.age_months)

@dataclass
class RemCardDTO:
    id: Optional[int]
    admission_id: int
    date_start: datetime
    date_end: datetime

@dataclass
class VitalDTO:
    id: Optional[int]
    admission_id: int
    timestamp: datetime
    sys: Optional[int] = None
    dia: Optional[int] = None
    pulse: Optional[int] = None
    temp: Optional[float] = None
    spo2: Optional[int] = None
    rr: Optional[int] = None
    cvp: Optional[int] = None
    last_modified_by: Optional[str] = None
    updated_at: Optional[str] = None
    revision: int = 0

@dataclass
class FluidDTO:
    id: Optional[int]
    admission_id: int
    timestamp: datetime
    iv_input: float = 0
    oral_input: float = 0
    food: float = 0
    urine: float = 0
    ng_output: float = 0
    drain_output: float = 0
    stool: float = 0
    other_output: float = 0
    last_modified_by: Optional[str] = None
    updated_at: Optional[str] = None
    revision: int = 0

@dataclass
class DietTemplateDTO:
    id: Optional[int] = None
    name: str = ""
    diet_text: str = ""
    schedule_json: str = "[]"
    is_default: int = 0
    version: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    last_modified_by: Optional[str] = None

@dataclass
class DietPlanDTO:
    id: Optional[int] = None
    admission_id: int = 0
    shift_start: datetime = field(default_factory=datetime.now)
    template_id: Optional[int] = None
    diet_text: str = ""
    schedule_json: str = "[]"
    version: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    last_modified_by: Optional[str] = None

@dataclass
class OralIntakeEventDTO:
    id: Optional[int] = None
    admission_id: int = 0
    shift_start: datetime = field(default_factory=datetime.now)
    event_time: datetime = field(default_factory=datetime.now)
    amount_ml: float = 0
    version: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    last_modified_by: Optional[str] = None

@dataclass
class AdministrationDTO:
    id: Optional[int] = None
    order_id: int = 0
    chain_id: Optional[str] = None
    big_chain_id: Optional[str] = None
    cell_role: str = "single"  # single, start, body, end
    planned_time: datetime = field(default_factory=datetime.now)
    actual_time: Optional[datetime] = None
    performer_id: Optional[int] = None
    status: str = "planned"  # planned, cancelled, deleted
    version: int = 0
    is_committed: int = 0
    comment: str = ""
    dose_given: Optional[float] = None
    volume_ml: float = 0.0
    last_modified_by: Optional[str] = None
    updated_at: Optional[str] = None

@dataclass
class OrderDTO:
    id: Optional[int] = None
    admission_id: int = 0
    drug_key: Optional[str] = None
    latin: str = ""
    type: OrderType = OrderType.MEDICATION
    status: OrderStatus = OrderStatus.ACTIVE
    
    # Дозировка
    dose_value: float = 0.0
    dose_unit: str = "mg"
    is_per_kg: bool = False
    
    # Расписание
    frequency: int = 1 # раз в сутки
    interval_hours: Optional[int] = None
    specific_times: List[str] = field(default_factory=list) # ["08:00", "20:00"]
    
    # Инфузия
    rate_ml_h: Optional[float] = None
    volume_total: Optional[float] = None
    duration_min: Optional[int] = None
    
    # Метаданные
    sort_order: int = 0
    draft_sort_order: Optional[int] = None
    is_finalized: bool = False
    is_committed: int = 0
    revision: int = 0
    created_at: datetime = field(default_factory=datetime.now)
    created_by: Optional[int] = None
    comment: str = ""
    last_modified_by: Optional[str] = None
    updated_at: Optional[str] = None
    
    # Связанные данные (не для БД напрямую)
    administrations: List[AdministrationDTO] = field(default_factory=list)

@dataclass
class VentilationCaseDTO:
    id: Optional[int] = None
    admission_id: int = 0
    episode_number: int = 0
    start_time: datetime = field(default_factory=datetime.now)
    end_time: Optional[datetime] = None
    type: str = "transfer"
    start_type: VentilationStartType = VentilationStartType.IN_DEPARTMENT
    delivery_type: VentilationDeliveryType = VentilationDeliveryType.UNKNOWN
    is_active: bool = True
    revision: int = 0


@dataclass
class VentilationEventDTO:
    id: Optional[int] = None
    admission_id: int = 0
    ivl_episode_id: int = 0
    timestamp: datetime = field(default_factory=datetime.now)
    event_type: VentilationEventType = VentilationEventType.MODE_CHANGE
    mode: Optional[VentilationMode] = None
    parameters: Dict[str, Any] = field(default_factory=dict)
    extubation_reason: Optional[str] = None
    o2_flow: Optional[float] = None
    author: Optional[str] = None
    revision: int = 0


@dataclass
class VentilationTubeDTO:
    id: Optional[int] = None
    admission_id: int = 0
    ivl_episode_id: int = 0
    device_type: str = "ENDOTRACHEAL_TUBE"
    insertion_time: datetime = field(default_factory=datetime.now)
    replacement_time: Optional[datetime] = None
    removal_time: Optional[datetime] = None
    location: Optional[str] = None

class ScheduleEngine:
    """Движок для преобразования правил в конкретные точки времени"""
    @staticmethod
    def generate_times(frequency: int, start_hour: int = 8) -> List[str]:
        """Генерирует список времени ["08:00", "20:00"] на основе кратности"""
        if frequency <= 0: return []
        interval = 24 // frequency
        times = []
        for i in range(frequency):
            hour = (start_hour + i * interval) % 24
            times.append(f"{hour:02d}:00")
        return sorted(times)
