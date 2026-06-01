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
        self.refresh_transfusion_statuses(int(admission_id))
        return self.dao.list_by_admission(int(admission_id))

    def get_transfusion_registration_sheet(
        self,
        admission_id: int,
        *,
        start_dt: Optional[datetime] = None,
        end_dt: Optional[datetime] = None,
    ) -> dict[str, Any]:
        self.refresh_transfusion_statuses(int(admission_id))
        snapshot = self._build_patient_snapshot(int(admission_id))
        raw_rows = self.dao.list_completed_transfusions_for_registration(
            int(admission_id),
            start_dt=start_dt,
            end_dt=end_dt,
        )
        rows = [self._transfusion_registration_row(row) for row in raw_rows]
        recipient_abo = self._registration_abo_label(self._first_non_empty(row.get("recipient_abo") for row in raw_rows))
        recipient_rh = self._registration_rh_label(self._first_non_empty(row.get("recipient_rh") for row in raw_rows))
        return {
            "patient_name": snapshot.get("full_name") or "",
            "history_number": snapshot.get("history_number") or "",
            "recipient_abo": recipient_abo,
            "recipient_rh": recipient_rh,
            "rows": rows,
        }

    def get_procedure_bundle(self, procedure_id: int) -> Optional[ProcedureBundle]:
        bundle = self.dao.get_bundle(int(procedure_id))
        if bundle and bundle.procedure.procedure_type == ProcedureType.TRANSFUSION.value:
            self.refresh_transfusion_statuses(bundle.procedure.admission_id)
            bundle = self.dao.get_bundle(int(procedure_id))
        return bundle

    def refresh_transfusion_statuses(self, admission_id: int):
        if not admission_id:
            return 0
        now = datetime.now().replace(second=0, microsecond=0)
        if not self.dao.has_transfusion_status_updates(int(admission_id), now=now):
            return 0

        def operation(cursor):
            return self.dao.refresh_transfusion_statuses(
                cursor,
                int(admission_id),
                now=now,
            )

        return self._run_write(f"procedure_transfusion_status_refresh:{admission_id}", operation)

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
        reagent_expirations = self._default_reagent_expirations(now.date())
        procedure = ProcedureDTO(
            admission_id=int(admission_id),
            patient_id=snapshot.get("patient_id"),
            procedure_type=ProcedureType.TRANSFUSION.value,
            status=ProcedureStatus.DRAFT.value,
            doctor_name_snapshot=doctor_name or "",
            department_snapshot=snapshot.get("department") or "",
            patient_snapshot_json=json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
            diagnosis_snapshot=snapshot.get("diagnosis") or "",
            created_by="doctor",
            updated_by="doctor",
        )
        transfusion = ProcedureTransfusionDTO(
            request_at=now,
            reagent_anti_a_expiration=reagent_expirations["anti_a"],
            reagent_anti_b_expiration=reagent_expirations["anti_b"],
            reagent_anti_d_expiration=reagent_expirations["anti_d"],
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
        self._apply_previous_transfusion_defaults(procedure, transfusion, consent)
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
        procedure.status = self._resolve_transfusion_status(procedure)
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

    def mark_protocols_printed(self, procedure_ids: list[int]):
        ids = [int(value) for value in procedure_ids if value]
        if not ids:
            return 0

        def operation(cursor):
            return self.dao.mark_protocols_printed(cursor, ids)

        return self._run_write("procedure_protocols_mark_printed", operation)

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

    @classmethod
    def _transfusion_registration_row(cls, row: dict[str, Any]) -> dict[str, str]:
        indication_code = str(row.get("indication_code") or "").strip()
        component = str(row.get("donor_component_name") or "").strip()
        is_plasma = cls._registration_is_plasma(indication_code, component)
        return {
            "date": cls._format_registration_date(row.get("started_at")),
            "indication": cls._registration_indication(indication_code),
            "method": "в/в капельно",
            "volume_ml": cls._plain_registration_value(row.get("volume_ml")),
            "component": cls._registration_component_name(component),
            "donor_abo": cls._registration_abo_label(row.get("donor_abo")),
            "donor_rh": cls._registration_rh_label(row.get("donor_rh")),
            "unit_number": cls._plain_registration_value(row.get("unit_number")),
            "collection_date": cls._plain_registration_value(row.get("collection_date")),
            "donor_code": cls._plain_registration_value(row.get("donor_code")),
            "compat_group": "" if is_plasma else cls._compatibility_short(row.get("plane_compatibility")),
            "compat_rh": "" if is_plasma else cls._compatibility_short(row.get("plane_compatibility")),
            "biological_test": "совм.",
            "reaction": cls._registration_reaction(row),
            "doctor": cls._plain_registration_value(row.get("operator_doctor_name") or row.get("doctor_name_snapshot")),
        }

    @staticmethod
    def _first_non_empty(values) -> str:
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return ""

    @staticmethod
    def _plain_registration_value(value: Any) -> str:
        return str(value or "").strip()

    @staticmethod
    def _format_registration_date(value: Any) -> str:
        if isinstance(value, datetime):
            return value.strftime("%d.%m.%Y")
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            return datetime.fromisoformat(text.replace(" ", "T")).strftime("%d.%m.%Y")
        except ValueError:
            return text[:10]

    @staticmethod
    def _registration_indication(indication_code: str) -> str:
        normalized = str(indication_code or "").strip().lower()
        if normalized == "voce":
            return "ВОЦЭ"
        if normalized == "vpfs":
            return "ВПФС"
        return str(indication_code or "").strip()

    @staticmethod
    def _registration_abo_label(value: Any) -> str:
        text = str(value or "").strip()
        normalized = text.upper().replace(" ", "")
        if normalized.startswith(("O(I)", "0(I)", "О(I)")):
            return "O(I) первая"
        if normalized.startswith("A(II)"):
            return "A(II) вторая"
        if normalized.startswith("B(III)"):
            return "B(III) третья"
        if normalized.startswith("AB(IV)"):
            return "AB(IV) четвертая"
        return text

    @staticmethod
    def _registration_rh_label(value: Any) -> str:
        text = str(value or "").strip()
        normalized = text.upper().replace(" ", "")
        if "RH(+)" in normalized or "RH+" in normalized or "ПОЛОЖ" in normalized:
            return "Rh(+) пол."
        if "RH(-)" in normalized or "RH-" in normalized or "ОТРИЦ" in normalized or "ОТР." in normalized:
            return "Rh(-) отр."
        return text

    @staticmethod
    def _registration_component_name(component: str) -> str:
        text = str(component or "").strip()
        normalized = text.lower().replace("ё", "е")
        if "сзп" in normalized or "плазм" in normalized:
            return "СЗП"
        if "взвес" in normalized:
            return "эр. взвесь"
        if "масс" in normalized:
            return "эр. масса"
        return text.replace("Эритроцитарная", "эр.").replace("эритроцитарная", "эр.").strip()

    @staticmethod
    def _registration_is_plasma(indication_code: str, component: str) -> bool:
        normalized_component = str(component or "").lower().replace("ё", "е")
        return str(indication_code or "").strip().lower() == "vpfs" or "сзп" in normalized_component or "плазм" in normalized_component

    @staticmethod
    def _compatibility_short(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return "совм."
        return "совм." if "совмест" in text.lower() else text

    @staticmethod
    def _registration_reaction(row: dict[str, Any]) -> str:
        symptoms = str(row.get("reaction_symptoms") or "").strip()
        severity = str(row.get("reaction_severity") or "").strip()
        return "да" if symptoms or severity else "нет"

    @staticmethod
    def _normalize_duration(procedure: ProcedureDTO):
        start = procedure.started_at
        finish = procedure.finished_at
        if start and finish:
            minutes = int(max(0, round((finish - start).total_seconds() / 60.0)))
            procedure.duration_minutes = minutes
        else:
            procedure.duration_minutes = None

    @staticmethod
    def _resolve_transfusion_status(procedure: ProcedureDTO, *, now: Optional[datetime] = None) -> str:
        start = procedure.started_at
        finish = procedure.finished_at
        if not start or not finish:
            return ProcedureStatus.DRAFT.value
        now = (now or datetime.now()).replace(second=0, microsecond=0)
        start = start.replace(second=0, microsecond=0)
        finish = finish.replace(second=0, microsecond=0)
        if now < start:
            return ProcedureStatus.DRAFT.value
        if now <= finish:
            return ProcedureStatus.ACTIVE.value
        return ProcedureStatus.COMPLETED.value

    @staticmethod
    def _apply_cvc_status(procedure: ProcedureDTO, cvc: ProcedureCvcDTO):
        action = str(cvc.removed_or_replaced or "").strip()
        if action == "removed":
            procedure.status = ProcedureStatus.CATHETER_REMOVED.value
            cvc.catheter_status = "removed"
            if cvc.removed_at is not None:
                procedure.finished_at = cvc.removed_at
                ProceduresService._normalize_duration(procedure)
        elif action == "replaced":
            procedure.status = ProcedureStatus.CATHETER_REPLACED.value
            cvc.catheter_status = "replaced"
            if cvc.removed_at is not None:
                procedure.finished_at = cvc.removed_at
                ProceduresService._normalize_duration(procedure)
        elif procedure.status == ProcedureStatus.CATHETER_TRANSFERRED.value:
            cvc.catheter_status = "transferred_with_catheter"
            cvc.removed_at = procedure.finished_at or cvc.removed_at
            ProceduresService._normalize_duration(procedure)
        elif procedure.status == ProcedureStatus.CATHETER_DEAD.value:
            cvc.catheter_status = "dead_with_catheter"
            cvc.removed_at = procedure.finished_at or cvc.removed_at
            ProceduresService._normalize_duration(procedure)
        elif procedure.status == ProcedureStatus.ACTIVE.value:
            cvc.catheter_status = "active"
            procedure.finished_at = None
            procedure.duration_minutes = None
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

    def _apply_previous_transfusion_defaults(
        self,
        procedure: ProcedureDTO,
        transfusion: ProcedureTransfusionDTO,
        consent: ProcedureConsentDTO,
    ) -> None:
        previous = self.dao.get_latest_completed_transfusion_bundle(
            patient_id=procedure.patient_id,
            admission_id=procedure.admission_id,
        )
        if not previous or not previous.transfusion:
            return

        previous_transfusion = previous.transfusion
        transfusion.recipient_abo = previous_transfusion.recipient_abo
        transfusion.recipient_rh = previous_transfusion.recipient_rh
        transfusion.recipient_antigens = previous_transfusion.recipient_antigens
        transfusion.alloimmune_antibodies = previous_transfusion.alloimmune_antibodies
        transfusion.transfusions_history = previous_transfusion.transfusions_history
        transfusion.reactions_history = previous_transfusion.reactions_history
        transfusion.reactions_history_details = (
            previous_transfusion.reactions_history_details
            if previous_transfusion.reactions_history == "yes"
            else ""
        )
        transfusion.individual_selection_history = previous_transfusion.individual_selection_history
        transfusion.donor_abo = previous_transfusion.donor_abo
        transfusion.donor_rh = previous_transfusion.donor_rh
        transfusion.donor_antigens = previous_transfusion.donor_antigens
        transfusion.reagent_anti_a_series = previous_transfusion.reagent_anti_a_series or "069F"
        transfusion.reagent_anti_b_series = previous_transfusion.reagent_anti_b_series or "070R"
        transfusion.reagent_anti_d_series = previous_transfusion.reagent_anti_d_series or "080"

        defaults = self._default_reagent_expirations(date.today())
        transfusion.reagent_anti_a_expiration = self._current_or_default_reagent_expiration(
            previous_transfusion.reagent_anti_a_expiration,
            defaults["anti_a"],
        )
        transfusion.reagent_anti_b_expiration = self._current_or_default_reagent_expiration(
            previous_transfusion.reagent_anti_b_expiration,
            defaults["anti_b"],
        )
        transfusion.reagent_anti_d_expiration = self._current_or_default_reagent_expiration(
            previous_transfusion.reagent_anti_d_expiration,
            defaults["anti_d"],
        )

        if previous.consent:
            consent.consent_kind = ConsentKind.TRANSFUSION_CONSENT.value
            consent.consent_mode = previous.consent.consent_mode or "patient"
            consent.patient_signed = int(previous.consent.patient_signed or 0)
            consent.representative_name = previous.consent.representative_name
            consent.representative_details = previous.consent.representative_details
            consent.consilium_json = previous.consent.consilium_json or "{}"
            consent.emergency_reason = previous.consent.emergency_reason

    @classmethod
    def _default_reagent_expirations(cls, reference_date: date) -> dict[str, str]:
        return {
            "anti_a": cls._format_date(cls._add_months(reference_date, 3) + timedelta(days=7)),
            "anti_b": cls._format_date(cls._add_months(reference_date, 3) + timedelta(days=7)),
            "anti_d": cls._format_date(cls._add_months(reference_date, 2) + timedelta(days=9)),
        }

    @classmethod
    def _current_or_default_reagent_expiration(cls, previous_value: str, default_value: str) -> str:
        previous_date = cls._parse_date(previous_value)
        if previous_date and previous_date >= date.today():
            return cls._format_date(previous_date)
        return default_value

    def _validate_transfusion_times(self, procedure: ProcedureDTO, transfusion: ProcedureTransfusionDTO) -> None:
        now = datetime.now().replace(second=59, microsecond=999999)
        if transfusion.request_at and transfusion.request_at > now:
            raise ValueError("Время подачи заявки не может быть в будущем.")

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
        admission_limited_times = [
            ("время подачи заявки", transfusion.request_at),
            ("начало трансфузии", procedure.started_at),
            ("окончание трансфузии", procedure.finished_at),
        ]
        for label, value in admission_limited_times:
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
