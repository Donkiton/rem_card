from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

from rem_card.data.dao.procedures_dao import ProceduresDAO
from rem_card.data.dto.procedures_dto import (
    ConsentKind,
    ProcedureBundle,
    ProcedureConsentDTO,
    ProcedureCvcDTO,
    ProcedureDTO,
    ProcedureLumbarPunctureDTO,
    ProcedureStatus,
    ProcedureTransfusionDTO,
    ProcedureType,
)


class ProceduresService:
    def __init__(self, dao: ProceduresDAO, data_service=None):
        self.dao = dao
        self.data_service = data_service

    def list_procedures(self, admission_id: int) -> list[ProcedureDTO]:
        return self.dao.list_by_admission(int(admission_id))

    def get_procedure_bundle(self, procedure_id: int) -> Optional[ProcedureBundle]:
        return self.dao.get_bundle(int(procedure_id))

    def create_empty_cvc(self, admission_id: int, *, doctor_name: str = "") -> ProcedureBundle:
        snapshot = self._build_patient_snapshot(admission_id)
        now = datetime.now().replace(second=0, microsecond=0)
        procedure = ProcedureDTO(
            admission_id=int(admission_id),
            patient_id=snapshot.get("patient_id"),
            procedure_type=ProcedureType.CVC.value,
            status=ProcedureStatus.DRAFT.value,
            started_at=now,
            finished_at=now,
            duration_minutes=0,
            doctor_name_snapshot=doctor_name or "",
            department_snapshot=snapshot.get("department") or "",
            patient_snapshot_json=json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
            diagnosis_snapshot=snapshot.get("diagnosis") or "",
            created_by="doctor",
            updated_by="doctor",
        )
        cvc = ProcedureCvcDTO(operator_doctor_name=doctor_name or "")
        consent = ProcedureConsentDTO(
            consent_kind=ConsentKind.CVC_CONSENT.value,
            consent_mode="patient",
            patient_signed=1,
            diagnosis_snapshot=procedure.diagnosis_snapshot,
            doctor_name_snapshot=doctor_name or "",
        )
        return ProcedureBundle(procedure=procedure, cvc=cvc, consent=consent, patient_snapshot=snapshot)

    def create_empty_lumbar_puncture(self, admission_id: int, *, doctor_name: str = "") -> ProcedureBundle:
        snapshot = self._build_patient_snapshot(admission_id)
        now = datetime.now().replace(second=0, microsecond=0)
        procedure = ProcedureDTO(
            admission_id=int(admission_id),
            patient_id=snapshot.get("patient_id"),
            procedure_type=ProcedureType.LUMBAR_PUNCTURE.value,
            status=ProcedureStatus.DRAFT.value,
            started_at=now,
            finished_at=now,
            duration_minutes=0,
            doctor_name_snapshot=doctor_name or "",
            department_snapshot=snapshot.get("department") or "",
            patient_snapshot_json=json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
            diagnosis_snapshot=snapshot.get("diagnosis") or "",
            created_by="doctor",
            updated_by="doctor",
        )
        lumbar_puncture = ProcedureLumbarPunctureDTO(operator_doctor_name=doctor_name or "")
        consent = ProcedureConsentDTO(
            consent_kind=ConsentKind.LUMBAR_PUNCTURE_CONSENT.value,
            consent_mode="patient",
            patient_signed=1,
            diagnosis_snapshot=procedure.diagnosis_snapshot,
            doctor_name_snapshot=doctor_name or "",
        )
        return ProcedureBundle(
            procedure=procedure,
            lumbar_puncture=lumbar_puncture,
            consent=consent,
            patient_snapshot=snapshot,
        )

    def create_empty_transfusion(self, admission_id: int, *, doctor_name: str = "") -> ProcedureBundle:
        snapshot = self._build_patient_snapshot(admission_id)
        now = datetime.now().replace(second=0, microsecond=0)
        procedure = ProcedureDTO(
            admission_id=int(admission_id),
            patient_id=snapshot.get("patient_id"),
            procedure_type=ProcedureType.TRANSFUSION.value,
            status=ProcedureStatus.DRAFT.value,
            started_at=now,
            finished_at=now,
            duration_minutes=0,
            doctor_name_snapshot=doctor_name or "",
            department_snapshot=snapshot.get("department") or "",
            patient_snapshot_json=json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
            diagnosis_snapshot=snapshot.get("diagnosis") or "",
            created_by="doctor",
            updated_by="doctor",
        )
        transfusion = ProcedureTransfusionDTO(
            request_at=now,
            reagent_anti_a_expiration=self._format_date(self._add_months(now.date(), 3) + timedelta(days=7)),
            reagent_anti_b_expiration=self._format_date(self._add_months(now.date(), 3) + timedelta(days=7)),
            reagent_anti_d_expiration=self._format_date(self._add_months(now.date(), 2) + timedelta(days=9)),
            observation_json=self.default_transfusion_observation_json(),
            operator_doctor_name=doctor_name or "",
        )
        consent = ProcedureConsentDTO(
            consent_kind=ConsentKind.TRANSFUSION_CONSENT.value,
            consent_mode="patient",
            patient_signed=1,
            diagnosis_snapshot=procedure.diagnosis_snapshot,
            doctor_name_snapshot=doctor_name or "",
        )
        return ProcedureBundle(
            procedure=procedure,
            transfusion=transfusion,
            consent=consent,
            patient_snapshot=snapshot,
        )

    def save_cvc_procedure(
        self,
        procedure: ProcedureDTO,
        cvc: ProcedureCvcDTO,
        consent: ProcedureConsentDTO,
    ) -> int:
        if procedure.procedure_type != ProcedureType.CVC.value:
            raise ValueError("Через этот метод можно сохранить только процедуру ЦВК.")
        if not procedure.admission_id:
            raise ValueError("Не указана госпитализация пациента.")

        if not procedure.patient_snapshot_json or procedure.patient_snapshot_json == "{}":
            snapshot = self._build_patient_snapshot(procedure.admission_id)
            procedure.patient_id = snapshot.get("patient_id")
            procedure.department_snapshot = procedure.department_snapshot or snapshot.get("department") or ""
            procedure.diagnosis_snapshot = procedure.diagnosis_snapshot or snapshot.get("diagnosis") or ""
            procedure.patient_snapshot_json = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))

        self._normalize_duration(procedure)
        self._apply_cvc_status(procedure, cvc)
        cvc.operator_doctor_name = cvc.operator_doctor_name or procedure.doctor_name_snapshot
        consent.diagnosis_snapshot = consent.diagnosis_snapshot or procedure.diagnosis_snapshot
        consent.doctor_name_snapshot = consent.doctor_name_snapshot or procedure.doctor_name_snapshot

        def operation(cursor):
            procedure_id = self.dao.save_procedure(cursor, procedure)
            cvc.procedure_id = procedure_id
            consent.procedure_id = procedure_id
            self.dao.save_cvc(cursor, cvc)
            self.dao.save_consent(cursor, consent)
            return procedure_id

        return int(self._run_write(f"procedure_cvc_save:{procedure.admission_id}", operation))

    def save_lumbar_puncture_procedure(
        self,
        procedure: ProcedureDTO,
        lumbar_puncture: ProcedureLumbarPunctureDTO,
        consent: ProcedureConsentDTO,
    ) -> int:
        if procedure.procedure_type != ProcedureType.LUMBAR_PUNCTURE.value:
            raise ValueError("Через этот метод можно сохранить только люмбальную пункцию.")
        if not procedure.admission_id:
            raise ValueError("Не указана госпитализация пациента.")

        if not procedure.patient_snapshot_json or procedure.patient_snapshot_json == "{}":
            snapshot = self._build_patient_snapshot(procedure.admission_id)
            procedure.patient_id = snapshot.get("patient_id")
            procedure.department_snapshot = procedure.department_snapshot or snapshot.get("department") or ""
            procedure.diagnosis_snapshot = procedure.diagnosis_snapshot or snapshot.get("diagnosis") or ""
            procedure.patient_snapshot_json = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))

        self._normalize_duration(procedure)
        lumbar_puncture.operator_doctor_name = lumbar_puncture.operator_doctor_name or procedure.doctor_name_snapshot
        consent.consent_kind = ConsentKind.LUMBAR_PUNCTURE_CONSENT.value
        consent.diagnosis_snapshot = consent.diagnosis_snapshot or procedure.diagnosis_snapshot
        consent.doctor_name_snapshot = consent.doctor_name_snapshot or procedure.doctor_name_snapshot

        def operation(cursor):
            procedure_id = self.dao.save_procedure(cursor, procedure)
            lumbar_puncture.procedure_id = procedure_id
            consent.procedure_id = procedure_id
            self.dao.save_lumbar_puncture(cursor, lumbar_puncture)
            self.dao.save_consent(cursor, consent)
            return procedure_id

        return int(self._run_write(f"procedure_lumbar_puncture_save:{procedure.admission_id}", operation))

    def save_transfusion_procedure(
        self,
        procedure: ProcedureDTO,
        transfusion: ProcedureTransfusionDTO,
        consent: ProcedureConsentDTO,
    ) -> int:
        if procedure.procedure_type != ProcedureType.TRANSFUSION.value:
            raise ValueError("Через этот метод можно сохранить только гемотрансфузию.")
        if not procedure.admission_id:
            raise ValueError("Не указана госпитализация пациента.")

        if not procedure.patient_snapshot_json or procedure.patient_snapshot_json == "{}":
            snapshot = self._build_patient_snapshot(procedure.admission_id)
            procedure.patient_id = snapshot.get("patient_id")
            procedure.department_snapshot = procedure.department_snapshot or snapshot.get("department") or ""
            procedure.diagnosis_snapshot = procedure.diagnosis_snapshot or snapshot.get("diagnosis") or ""
            procedure.patient_snapshot_json = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))

        self._normalize_duration(procedure)
        self._validate_transfusion_times(procedure, transfusion)
        self._validate_transfusion_expiration(transfusion)
        transfusion.operator_doctor_name = transfusion.operator_doctor_name or procedure.doctor_name_snapshot
        consent.consent_kind = ConsentKind.TRANSFUSION_CONSENT.value
        consent.diagnosis_snapshot = consent.diagnosis_snapshot or procedure.diagnosis_snapshot
        consent.doctor_name_snapshot = consent.doctor_name_snapshot or procedure.doctor_name_snapshot

        def operation(cursor):
            procedure_id = self.dao.save_procedure(cursor, procedure)
            transfusion.procedure_id = procedure_id
            consent.procedure_id = procedure_id
            self.dao.save_transfusion(cursor, transfusion)
            self.dao.save_consent(cursor, consent)
            return procedure_id

        return int(self._run_write(f"procedure_transfusion_save:{procedure.admission_id}", operation))

    def cancel_procedure(self, procedure_id: int, *, updated_by: str = "doctor"):
        def operation(cursor):
            self.dao.cancel_procedure(cursor, int(procedure_id), updated_by=updated_by)
            return int(procedure_id)

        return self._run_write(f"procedure_cancel:{procedure_id}", operation)

    def _run_write(self, description: str, operation: Callable):
        if self.data_service:
            return self.data_service.run_write(description, operation)
        return self.dao.db.run_write_operation(operation, source=description)

    def _build_patient_snapshot(self, admission_id: int) -> dict[str, Any]:
        row = self.dao.get_patient_snapshot_source(int(admission_id))
        if not row:
            raise ValueError("Пациент для процедуры не найден.")

        full_name = row.get("full_name") or " ".join(
            part for part in (row.get("last_name"), row.get("first_name"), row.get("middle_name")) if part
        )
        department = row.get("department_profile") or row.get("source_department") or ""
        return {
            "patient_id": row.get("patient_id"),
            "admission_id": row.get("admission_id"),
            "admission_datetime": row.get("admission_datetime") or "",
            "full_name": full_name or "Неизвестно",
            "sex": row.get("patient_gender") or "",
            "age": row.get("patient_age"),
            "age_months": row.get("patient_months"),
            "age_unit": row.get("patient_age_unit") or "л",
            "birth_date": row.get("birth_date") or "",
            "history_number": row.get("history_number") or "",
            "department": department,
            "bed_number": row.get("bed_number"),
            "diagnosis": row.get("diagnosis_text") or "",
            "diagnosis_code": row.get("diagnosis_code") or "",
        }

    @staticmethod
    def _normalize_duration(procedure: ProcedureDTO):
        start = procedure.started_at
        finish = procedure.finished_at
        if start and finish:
            minutes = int(max(0, round((finish - start).total_seconds() / 60.0)))
            procedure.duration_minutes = minutes

    @staticmethod
    def _apply_cvc_status(procedure: ProcedureDTO, cvc: ProcedureCvcDTO):
        action = str(cvc.removed_or_replaced or "").strip()
        if action == "removed":
            procedure.status = ProcedureStatus.CATHETER_REMOVED.value
            cvc.catheter_status = "removed"
        elif action == "replaced":
            procedure.status = ProcedureStatus.CATHETER_REPLACED.value
            cvc.catheter_status = "replaced"
        elif not cvc.catheter_status:
            cvc.catheter_status = "active"

    @staticmethod
    def default_transfusion_observation_json() -> str:
        payload = {
            "before": {"bp": "", "pulse": "", "temp": "", "diuresis": "сохранен, желтая"},
            "hour1": {"bp": "", "pulse": "", "temp": "", "diuresis": "сохранен, желтая"},
            "hour2": {"bp": "", "pulse": "", "temp": "", "diuresis": "сохранен, желтая"},
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _validate_transfusion_times(self, procedure: ProcedureDTO, transfusion: ProcedureTransfusionDTO) -> None:
        now = datetime.now().replace(second=59, microsecond=999999)
        times = [
            ("время подачи заявки", transfusion.request_at),
            ("начало трансфузии", procedure.started_at),
            ("окончание трансфузии", procedure.finished_at),
        ]
        for label, value in times:
            if value and value > now:
                raise ValueError(f"{label.capitalize()} не может быть в будущем.")

        if procedure.started_at and procedure.finished_at and procedure.started_at > procedure.finished_at:
            raise ValueError("Начало трансфузии не может быть позже окончания.")

        snapshot = {}
        try:
            snapshot = json.loads(procedure.patient_snapshot_json or "{}")
        except Exception:
            snapshot = {}
        admission_dt = self._parse_snapshot_datetime(snapshot.get("admission_datetime"))
        if not admission_dt:
            return
        for label, value in times:
            if value and value < admission_dt.replace(second=0, microsecond=0):
                raise ValueError(
                    f"{label.capitalize()} не может быть раньше поступления пациента "
                    f"({admission_dt.strftime('%d.%m.%Y %H:%M')})."
                )

    @classmethod
    def _validate_transfusion_expiration(cls, transfusion: ProcedureTransfusionDTO) -> None:
        expiration = cls._parse_date(transfusion.expiration_date)
        if expiration and expiration < date.today():
            raise ValueError(
                "Срок годности компонента крови истек. "
                "Трансфузия невозможна: исправьте срок годности или дату заготовки."
            )

    @staticmethod
    def _parse_snapshot_datetime(value) -> Optional[datetime]:
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value).replace(" ", "T"))
        except ValueError:
            return None

    @staticmethod
    def _parse_date(value) -> Optional[date]:
        text = str(value or "").strip()
        if not text:
            return None
        for fmt in ("%d.%m.%Y", "%d.%m.%y"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        return None

    @staticmethod
    def _format_date(value: date) -> str:
        return value.strftime("%d.%m.%Y")

    @staticmethod
    def _add_months(value: date, months: int) -> date:
        month_index = value.month - 1 + int(months)
        year = value.year + month_index // 12
        month = month_index % 12 + 1
        days_in_month = (
            date(year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1)
            - timedelta(days=1)
        ).day
        return value.replace(year=year, month=month, day=min(value.day, days_in_month))
