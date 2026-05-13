from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from rem_card.data.dto.procedures_dto import (
    ConsentKind,
    ProcedureBundle,
    ProcedureConsentDTO,
    ProcedureCvcDTO,
    ProcedureDTO,
    ProcedureLumbarPunctureDTO,
    ProcedureStatus,
    ProcedureType,
)


class ProceduresDAO:
    def __init__(self, db_manager):
        self.db = db_manager

    @staticmethod
    def _parse_dt(value: Any) -> Optional[datetime]:
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            return value
        text = str(value).replace(" ", "T")
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            if "." in text:
                head, frac = text.split(".", 1)
                return datetime.fromisoformat(f"{head}.{frac[:6]}")
            raise

    @staticmethod
    def _dt_value(value: Optional[datetime]) -> Optional[str]:
        return value.isoformat(timespec="seconds") if value else None

    @staticmethod
    def _json_dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _json_load(value: Any, default: Any):
        if not value:
            return default
        try:
            decoded = json.loads(str(value))
        except Exception:
            return default
        return decoded if isinstance(decoded, type(default)) else default

    @staticmethod
    def _row_dict(row) -> dict[str, Any]:
        return dict(row) if row is not None else {}

    def get_patient_snapshot_source(self, admission_id: int) -> Optional[dict[str, Any]]:
        row = self.db.fetch_one_remcard(
            """
            SELECT
                a.id AS admission_id,
                a.patient_id AS patient_id,
                a.history_number AS history_number,
                a.admission_datetime AS admission_datetime,
                a.bed_number AS bed_number,
                a.patient_age AS patient_age,
                a.patient_months AS patient_months,
                a.patient_age_unit AS patient_age_unit,
                a.patient_gender AS patient_gender,
                a.diagnosis_code AS diagnosis_code,
                a.diagnosis_text AS diagnosis_text,
                a.department_profile AS department_profile,
                a.source_department AS source_department,
                p.full_name AS full_name,
                p.last_name AS last_name,
                p.first_name AS first_name,
                p.middle_name AS middle_name,
                p.birth_date AS birth_date
            FROM admissions a
            JOIN patients p ON p.id = a.patient_id
            WHERE a.id = ?
            """,
            (int(admission_id),),
        )
        return self._row_dict(row) if row else None

    def list_by_admission(self, admission_id: int) -> list[ProcedureDTO]:
        rows = self.db.fetch_all_remcard(
            """
            SELECT *
            FROM procedures
            WHERE admission_id = ?
              AND COALESCE(is_deleted, 0) = 0
            ORDER BY COALESCE(started_at, created_at) DESC, id DESC
            """,
            (int(admission_id),),
        )
        return [self._map_procedure(row) for row in rows]

    def get_procedure(self, procedure_id: int) -> Optional[ProcedureDTO]:
        row = self.db.fetch_one_remcard("SELECT * FROM procedures WHERE id = ?", (int(procedure_id),))
        return self._map_procedure(row) if row else None

    def get_bundle(self, procedure_id: int) -> Optional[ProcedureBundle]:
        procedure = self.get_procedure(procedure_id)
        if not procedure:
            return None
        cvc = None
        lumbar_puncture = None
        consent = None
        if procedure.procedure_type == ProcedureType.CVC.value:
            cvc = self.get_cvc(procedure_id)
            consent = self.get_consent(procedure_id, ConsentKind.CVC_CONSENT.value)
        elif procedure.procedure_type == ProcedureType.LUMBAR_PUNCTURE.value:
            lumbar_puncture = self.get_lumbar_puncture(procedure_id)
            consent = self.get_consent(procedure_id, ConsentKind.LUMBAR_PUNCTURE_CONSENT.value)
        snapshot = self._json_load(procedure.patient_snapshot_json, {})
        return ProcedureBundle(
            procedure=procedure,
            cvc=cvc,
            lumbar_puncture=lumbar_puncture,
            consent=consent,
            patient_snapshot=snapshot,
        )

    def save_procedure(self, cursor, dto: ProcedureDTO) -> int:
        if dto.id is None:
            cursor.execute(
                """
                INSERT INTO procedures (
                    patient_id, admission_id, procedure_type, status,
                    started_at, finished_at, duration_minutes, doctor_id,
                    doctor_name_snapshot, department_snapshot, patient_snapshot_json,
                    diagnosis_snapshot, notes, created_by, updated_by, is_deleted
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dto.patient_id,
                    int(dto.admission_id),
                    dto.procedure_type,
                    dto.status,
                    self._dt_value(dto.started_at),
                    self._dt_value(dto.finished_at),
                    dto.duration_minutes,
                    dto.doctor_id,
                    dto.doctor_name_snapshot,
                    dto.department_snapshot,
                    dto.patient_snapshot_json,
                    dto.diagnosis_snapshot,
                    dto.notes,
                    dto.created_by or "doctor",
                    dto.updated_by or "doctor",
                    int(dto.is_deleted or 0),
                ),
            )
            dto.id = int(cursor.lastrowid)
            return int(dto.id)

        cursor.execute(
            """
            UPDATE procedures
            SET status = ?,
                started_at = ?,
                finished_at = ?,
                duration_minutes = ?,
                doctor_id = ?,
                doctor_name_snapshot = ?,
                department_snapshot = ?,
                diagnosis_snapshot = ?,
                notes = ?,
                updated_by = ?,
                revision = COALESCE(revision, 0) + 1,
                updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
            WHERE id = ?
            """,
            (
                dto.status,
                self._dt_value(dto.started_at),
                self._dt_value(dto.finished_at),
                dto.duration_minutes,
                dto.doctor_id,
                dto.doctor_name_snapshot,
                dto.department_snapshot,
                dto.diagnosis_snapshot,
                dto.notes,
                dto.updated_by or "doctor",
                int(dto.id),
            ),
        )
        return int(dto.id)

    def save_cvc(self, cursor, dto: ProcedureCvcDTO):
        cursor.execute(
            """
            INSERT INTO procedure_cvc (
                procedure_id, cvc_code_main_selected, cvc_code_tunneled_selected,
                indications_json, procedure_place_code, procedure_place_other,
                anesthesia_code, anesthesia_other, access_code, access_other,
                method_code, method_other, ultrasound_control_json,
                attempts_count, diameter_f, length_cm, lumens_count,
                fixation_json, fixation_other, position_confirmed_at,
                position_confirmation_json, technical_difficulty_code,
                technical_difficulty_description, actions_taken, catheter_status,
                removed_or_replaced, removed_at, usage_complications_code,
                usage_complications_description, additional_treatment,
                operator_doctor_name, removal_doctor_name, revision
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(procedure_id) DO UPDATE SET
                cvc_code_main_selected = excluded.cvc_code_main_selected,
                cvc_code_tunneled_selected = excluded.cvc_code_tunneled_selected,
                indications_json = excluded.indications_json,
                procedure_place_code = excluded.procedure_place_code,
                procedure_place_other = excluded.procedure_place_other,
                anesthesia_code = excluded.anesthesia_code,
                anesthesia_other = excluded.anesthesia_other,
                access_code = excluded.access_code,
                access_other = excluded.access_other,
                method_code = excluded.method_code,
                method_other = excluded.method_other,
                ultrasound_control_json = excluded.ultrasound_control_json,
                attempts_count = excluded.attempts_count,
                diameter_f = excluded.diameter_f,
                length_cm = excluded.length_cm,
                lumens_count = excluded.lumens_count,
                fixation_json = excluded.fixation_json,
                fixation_other = excluded.fixation_other,
                position_confirmed_at = excluded.position_confirmed_at,
                position_confirmation_json = excluded.position_confirmation_json,
                technical_difficulty_code = excluded.technical_difficulty_code,
                technical_difficulty_description = excluded.technical_difficulty_description,
                actions_taken = excluded.actions_taken,
                catheter_status = excluded.catheter_status,
                removed_or_replaced = excluded.removed_or_replaced,
                removed_at = excluded.removed_at,
                usage_complications_code = excluded.usage_complications_code,
                usage_complications_description = excluded.usage_complications_description,
                additional_treatment = excluded.additional_treatment,
                operator_doctor_name = excluded.operator_doctor_name,
                removal_doctor_name = excluded.removal_doctor_name,
                revision = COALESCE(procedure_cvc.revision, 0) + 1
            """,
            (
                int(dto.procedure_id),
                int(dto.cvc_code_main_selected or 0),
                int(dto.cvc_code_tunneled_selected or 0),
                self._json_dump({"selected": dto.indications, "other": dto.indications_other}),
                dto.procedure_place_code,
                dto.procedure_place_other,
                dto.anesthesia_code,
                dto.anesthesia_other,
                dto.access_code,
                dto.access_other,
                dto.method_code,
                dto.method_other,
                self._json_dump(dto.ultrasound_control),
                dto.attempts_count,
                dto.diameter_f,
                dto.length_cm,
                dto.lumens_count,
                self._json_dump(dto.fixation),
                dto.fixation_other,
                self._dt_value(dto.position_confirmed_at),
                self._json_dump(
                    {
                        "selected": dto.position_confirmation,
                        "comment": dto.position_confirmation_comment,
                    }
                ),
                dto.technical_difficulty_code,
                dto.technical_difficulty_description,
                dto.actions_taken,
                dto.catheter_status,
                dto.removed_or_replaced,
                self._dt_value(dto.removed_at),
                dto.usage_complications_code,
                dto.usage_complications_description,
                dto.additional_treatment,
                dto.operator_doctor_name,
                dto.removal_doctor_name,
            ),
        )

    def get_cvc(self, procedure_id: int) -> Optional[ProcedureCvcDTO]:
        row = self.db.fetch_one_remcard(
            "SELECT * FROM procedure_cvc WHERE procedure_id = ?",
            (int(procedure_id),),
        )
        return self._map_cvc(row) if row else None

    def save_lumbar_puncture(self, cursor, dto: ProcedureLumbarPunctureDTO):
        cursor.execute(
            """
            INSERT INTO procedure_lumbar_puncture (
                procedure_id, indications_json, procedure_place_code,
                procedure_place_other, anesthesia_code, anesthesia_other,
                access_code, access_other, level_code, level_other,
                technical_difficulty_code, technical_difficulty_description,
                actions_taken, result_code, csf_characteristics, result_notes,
                operator_doctor_name, revision
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(procedure_id) DO UPDATE SET
                indications_json = excluded.indications_json,
                procedure_place_code = excluded.procedure_place_code,
                procedure_place_other = excluded.procedure_place_other,
                anesthesia_code = excluded.anesthesia_code,
                anesthesia_other = excluded.anesthesia_other,
                access_code = excluded.access_code,
                access_other = excluded.access_other,
                level_code = excluded.level_code,
                level_other = excluded.level_other,
                technical_difficulty_code = excluded.technical_difficulty_code,
                technical_difficulty_description = excluded.technical_difficulty_description,
                actions_taken = excluded.actions_taken,
                result_code = excluded.result_code,
                csf_characteristics = excluded.csf_characteristics,
                result_notes = excluded.result_notes,
                operator_doctor_name = excluded.operator_doctor_name,
                revision = COALESCE(procedure_lumbar_puncture.revision, 0) + 1
            """,
            (
                int(dto.procedure_id),
                self._json_dump({"selected": dto.indications, "other": dto.indications_other}),
                dto.procedure_place_code,
                dto.procedure_place_other,
                dto.anesthesia_code,
                dto.anesthesia_other,
                dto.access_code,
                dto.access_other,
                dto.level_code,
                dto.level_other,
                dto.technical_difficulty_code,
                dto.technical_difficulty_description,
                dto.actions_taken,
                dto.result_code,
                dto.csf_characteristics,
                dto.result_notes,
                dto.operator_doctor_name,
            ),
        )

    def get_lumbar_puncture(self, procedure_id: int) -> Optional[ProcedureLumbarPunctureDTO]:
        row = self.db.fetch_one_remcard(
            "SELECT * FROM procedure_lumbar_puncture WHERE procedure_id = ?",
            (int(procedure_id),),
        )
        return self._map_lumbar_puncture(row) if row else None

    def save_consent(self, cursor, dto: ProcedureConsentDTO):
        if dto.id is None:
            cursor.execute(
                """
                SELECT id
                FROM procedure_consents
                WHERE procedure_id = ? AND consent_kind = ?
                LIMIT 1
                """,
                (int(dto.procedure_id), dto.consent_kind),
            )
            row = cursor.fetchone()
            if row:
                dto.id = int(row["id"])

        if dto.id is None:
            cursor.execute(
                """
                INSERT INTO procedure_consents (
                    procedure_id, consent_kind, consent_mode, patient_signed,
                    representative_name, representative_details, diagnosis_snapshot,
                    doctor_name_snapshot, consilium_json, emergency_reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(dto.procedure_id),
                    dto.consent_kind,
                    dto.consent_mode,
                    int(dto.patient_signed or 0),
                    dto.representative_name,
                    dto.representative_details,
                    dto.diagnosis_snapshot,
                    dto.doctor_name_snapshot,
                    dto.consilium_json,
                    dto.emergency_reason,
                ),
            )
            dto.id = int(cursor.lastrowid)
            return

        cursor.execute(
            """
            UPDATE procedure_consents
            SET consent_mode = ?,
                patient_signed = ?,
                representative_name = ?,
                representative_details = ?,
                diagnosis_snapshot = ?,
                doctor_name_snapshot = ?,
                consilium_json = ?,
                emergency_reason = ?,
                revision = COALESCE(revision, 0) + 1
            WHERE id = ?
            """,
            (
                dto.consent_mode,
                int(dto.patient_signed or 0),
                dto.representative_name,
                dto.representative_details,
                dto.diagnosis_snapshot,
                dto.doctor_name_snapshot,
                dto.consilium_json,
                dto.emergency_reason,
                int(dto.id),
            ),
        )

    def get_consent(self, procedure_id: int, consent_kind: str) -> Optional[ProcedureConsentDTO]:
        row = self.db.fetch_one_remcard(
            """
            SELECT *
            FROM procedure_consents
            WHERE procedure_id = ? AND consent_kind = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(procedure_id), consent_kind),
        )
        return self._map_consent(row) if row else None

    def cancel_procedure(self, cursor, procedure_id: int, *, updated_by: str = "doctor"):
        del updated_by
        cursor.execute("DELETE FROM procedure_cvc WHERE procedure_id = ?", (int(procedure_id),))
        cursor.execute("DELETE FROM procedure_lumbar_puncture WHERE procedure_id = ?", (int(procedure_id),))
        cursor.execute("DELETE FROM procedure_consents WHERE procedure_id = ?", (int(procedure_id),))
        cursor.execute("DELETE FROM procedures WHERE id = ?", (int(procedure_id),))

    def _map_procedure(self, row) -> ProcedureDTO:
        r = self._row_dict(row)
        return ProcedureDTO(
            id=r.get("id"),
            patient_id=r.get("patient_id"),
            admission_id=int(r.get("admission_id") or 0),
            procedure_type=r.get("procedure_type") or ProcedureType.CVC.value,
            status=r.get("status") or ProcedureStatus.DRAFT.value,
            created_at=self._parse_dt(r.get("created_at")),
            updated_at=self._parse_dt(r.get("updated_at")),
            started_at=self._parse_dt(r.get("started_at")),
            finished_at=self._parse_dt(r.get("finished_at")),
            duration_minutes=r.get("duration_minutes"),
            doctor_id=r.get("doctor_id"),
            doctor_name_snapshot=r.get("doctor_name_snapshot") or "",
            department_snapshot=r.get("department_snapshot") or "",
            patient_snapshot_json=r.get("patient_snapshot_json") or "{}",
            diagnosis_snapshot=r.get("diagnosis_snapshot") or "",
            notes=r.get("notes") or "",
            created_by=r.get("created_by") or "doctor",
            updated_by=r.get("updated_by") or "doctor",
            revision=int(r.get("revision") or 0),
            is_deleted=int(r.get("is_deleted") or 0),
        )

    def _map_consent(self, row) -> ProcedureConsentDTO:
        r = self._row_dict(row)
        return ProcedureConsentDTO(
            id=r.get("id"),
            procedure_id=int(r.get("procedure_id") or 0),
            consent_kind=r.get("consent_kind") or ConsentKind.CVC_CONSENT.value,
            consent_mode=r.get("consent_mode") or "patient",
            patient_signed=int(r.get("patient_signed") or 0),
            representative_name=r.get("representative_name") or "",
            representative_details=r.get("representative_details") or "",
            diagnosis_snapshot=r.get("diagnosis_snapshot") or "",
            doctor_name_snapshot=r.get("doctor_name_snapshot") or "",
            consilium_json=r.get("consilium_json") or "{}",
            emergency_reason=r.get("emergency_reason") or "",
            created_at=self._parse_dt(r.get("created_at")),
            printed_at=self._parse_dt(r.get("printed_at")),
            revision=int(r.get("revision") or 0),
        )

    def _map_cvc(self, row) -> ProcedureCvcDTO:
        r = self._row_dict(row)
        indications = self._json_load(r.get("indications_json"), {})
        position_confirmation = self._json_load(r.get("position_confirmation_json"), {})
        return ProcedureCvcDTO(
            procedure_id=int(r.get("procedure_id") or 0),
            cvc_code_main_selected=int(r.get("cvc_code_main_selected") or 0),
            cvc_code_tunneled_selected=int(r.get("cvc_code_tunneled_selected") or 0),
            indications=list(indications.get("selected") or []),
            indications_other=str(indications.get("other") or ""),
            procedure_place_code=r.get("procedure_place_code") or "",
            procedure_place_other=r.get("procedure_place_other") or "",
            anesthesia_code=r.get("anesthesia_code") or "",
            anesthesia_other=r.get("anesthesia_other") or "",
            access_code=r.get("access_code") or "",
            access_other=r.get("access_other") or "",
            method_code=r.get("method_code") or "",
            method_other=r.get("method_other") or "",
            ultrasound_control=list(self._json_load(r.get("ultrasound_control_json"), [])),
            attempts_count=r.get("attempts_count"),
            diameter_f=r.get("diameter_f"),
            length_cm=r.get("length_cm"),
            lumens_count=r.get("lumens_count"),
            fixation=list(self._json_load(r.get("fixation_json"), [])),
            fixation_other=r.get("fixation_other") or "",
            position_confirmed_at=self._parse_dt(r.get("position_confirmed_at")),
            position_confirmation=list(position_confirmation.get("selected") or []),
            position_confirmation_comment=str(position_confirmation.get("comment") or ""),
            technical_difficulty_code=r.get("technical_difficulty_code") or "none",
            technical_difficulty_description=r.get("technical_difficulty_description") or "",
            actions_taken=r.get("actions_taken") or "",
            catheter_status=r.get("catheter_status") or "active",
            removed_or_replaced=r.get("removed_or_replaced") or "",
            removed_at=self._parse_dt(r.get("removed_at")),
            usage_complications_code=r.get("usage_complications_code") or "none",
            usage_complications_description=r.get("usage_complications_description") or "",
            additional_treatment=r.get("additional_treatment") or "",
            operator_doctor_name=r.get("operator_doctor_name") or "",
            removal_doctor_name=r.get("removal_doctor_name") or "",
            revision=int(r.get("revision") or 0),
        )

    def _map_lumbar_puncture(self, row) -> ProcedureLumbarPunctureDTO:
        r = self._row_dict(row)
        indications = self._json_load(r.get("indications_json"), {})
        return ProcedureLumbarPunctureDTO(
            procedure_id=int(r.get("procedure_id") or 0),
            indications=list(indications.get("selected") or []),
            indications_other=str(indications.get("other") or ""),
            procedure_place_code=r.get("procedure_place_code") or "",
            procedure_place_other=r.get("procedure_place_other") or "",
            anesthesia_code=r.get("anesthesia_code") or "",
            anesthesia_other=r.get("anesthesia_other") or "",
            access_code=r.get("access_code") or "",
            access_other=r.get("access_other") or "",
            level_code=r.get("level_code") or "",
            level_other=r.get("level_other") or "",
            technical_difficulty_code=r.get("technical_difficulty_code") or "none",
            technical_difficulty_description=r.get("technical_difficulty_description") or "",
            actions_taken=r.get("actions_taken") or "",
            result_code=r.get("result_code") or "csf_obtained",
            csf_characteristics=r.get("csf_characteristics") or "",
            result_notes=r.get("result_notes") or "",
            operator_doctor_name=r.get("operator_doctor_name") or "",
            revision=int(r.get("revision") or 0),
        )
