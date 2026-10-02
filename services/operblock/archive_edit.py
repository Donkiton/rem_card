"""Edits for immutable-in-workflow, closed OperBlock archive cases."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from rem_card.services.concurrency import DataConflictError, DATA_CONFLICT_MESSAGE
from .common import OperBlockConflictError, _now_text, validate_operblock_runtime_path


_CASE_FIELDS = (
    "planned_operation_name", "planned_anesthesia_assistance_type", "planned_surgeons_json",
    "planned_operating_nurse", "planned_anesthesiologist", "planned_anesthetist",
    "height_cm", "weight_kg", "allergies", "blood_group", "blood_rh",
    "preop_sys", "preop_dia", "preop_pulse", "preop_spo2", "transfer_department",
)
_ADMISSION_FIELDS = ("history_number", "patient_gender", "diagnosis_code", "diagnosis_text", "department_profile")
_PATIENT_FIELDS = ("full_name", "birth_date", "last_name", "first_name", "middle_name")


def _ensure_history(cursor: sqlite3.Cursor) -> None:
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS operblock_archive_edit_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_case_id INTEGER NOT NULL,
            admission_id INTEGER NOT NULL,
            expected_case_revision INTEGER,
            expected_admission_revision INTEGER,
            before_json TEXT NOT NULL,
            after_json TEXT NOT NULL,
            edited_at TEXT NOT NULL,
            edited_by TEXT NOT NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_operblock_archive_edit_history_case ON operblock_archive_edit_history(operation_case_id, id DESC)")


class OperBlockArchiveEditMixin:
    def update_archived_operation_case_form_data(
        self, operation_case_id: int, data: dict[str, Any], *,
        expected_operation_case_revision: int | None, expected_admission_revision: int | None,
    ) -> dict[str, int]:
        """Update metadata of a closed case without restoring its table assignment."""
        validate_operblock_runtime_path(self.db)
        payload = dict(data or {})
        aliases = {
            "operation_name": "planned_operation_name", "anesthesia_assistance_type": "planned_anesthesia_assistance_type",
            "operating_nurse": "planned_operating_nurse", "anesthesiologist": "planned_anesthesiologist",
            "anesthetist": "planned_anesthetist", "gender": "patient_gender",
        }
        for source, target in aliases.items():
            if source in payload:
                payload[target] = payload[source]
        if "surgeons" in payload:
            payload["planned_surgeons_json"] = json.dumps(list(payload["surgeons"] or []), ensure_ascii=False)
        now = _now_text()

        def operation(cursor: sqlite3.Cursor):
            _ensure_history(cursor)
            row = cursor.execute("""
                SELECT oc.*, a.revision AS admission_revision, p.full_name, p.birth_date,
                       p.last_name, p.first_name, p.middle_name, a.history_number,
                       a.patient_gender, a.diagnosis_code, a.diagnosis_text, a.department_profile
                FROM operation_cases oc JOIN admissions a ON a.id=oc.admission_id
                JOIN patients p ON p.id=oc.patient_id WHERE oc.id=?
            """, (int(operation_case_id),)).fetchone()
            if not row:
                raise OperBlockConflictError("Архивный случай не найден.")
            before = dict(row)
            if str(before.get("status") or "") != "closed":
                raise OperBlockConflictError("Редактировать можно только закрытый архивный случай.")
            if (
                str(getattr(getattr(self.db, "runtime_context", None), "mode", "") or "") == "opblock_offline"
                and (str(before.get("migration_status") or "") == "verified" or before.get("migrated_at"))
            ):
                raise OperBlockConflictError("Подтверждённый локальный случай редактируется только в центральном архиве.")
            if expected_operation_case_revision is None or expected_admission_revision is None:
                raise DataConflictError(DATA_CONFLICT_MESSAGE)
            if int(before.get("revision") or 0) != int(expected_operation_case_revision) or int(before.get("admission_revision") or 0) != int(expected_admission_revision):
                raise DataConflictError(DATA_CONFLICT_MESSAGE)
            case_updates = {key: payload[key] for key in _CASE_FIELDS if key in payload}
            admission_updates = {key: payload[key] for key in _ADMISSION_FIELDS if key in payload}
            patient_updates = {key: payload[key] for key in _PATIENT_FIELDS if key in payload}
            if not (case_updates or admission_updates or patient_updates):
                return {"operation_case_id": int(operation_case_id), "revision": int(before.get("revision") or 0)}
            if patient_updates:
                sets = ", ".join(f'"{key}"=?' for key in patient_updates)
                cursor.execute(f"UPDATE patients SET {sets} WHERE id=?", (*patient_updates.values(), int(before["patient_id"])))
            if admission_updates:
                sets = ", ".join(f'"{key}"=?' for key in admission_updates)
                cursor.execute(f"UPDATE admissions SET {sets}, updated_at=?, revision=COALESCE(revision,0)+1 WHERE id=? AND COALESCE(revision,0)=?", (*admission_updates.values(), now, int(before["admission_id"]), int(expected_admission_revision)))
                if cursor.rowcount != 1: raise DataConflictError(DATA_CONFLICT_MESSAGE)
            if case_updates:
                sets = ", ".join(f'"{key}"=?' for key in case_updates)
                cursor.execute(f"UPDATE operation_cases SET {sets}, last_modified_by='operblock_archive', revision=COALESCE(revision,0)+1 WHERE id=? AND status='closed' AND COALESCE(revision,0)=?", (*case_updates.values(), int(operation_case_id), int(expected_operation_case_revision)))
                if cursor.rowcount != 1: raise DataConflictError(DATA_CONFLICT_MESSAGE)
            else:
                cursor.execute("UPDATE operation_cases SET last_modified_by='operblock_archive', revision=COALESCE(revision,0)+1 WHERE id=? AND status='closed' AND COALESCE(revision,0)=?", (int(operation_case_id), int(expected_operation_case_revision)))
                if cursor.rowcount != 1: raise DataConflictError(DATA_CONFLICT_MESSAGE)
            after = cursor.execute("SELECT * FROM operation_cases WHERE id=?", (int(operation_case_id),)).fetchone()
            patient = cursor.execute("SELECT * FROM patients WHERE id=?", (int(before["patient_id"]),)).fetchone()
            admission = cursor.execute("SELECT * FROM admissions WHERE id=?", (int(before["admission_id"]),)).fetchone()
            before_snapshot = {"case": before, "patient": {key: before.get(key) for key in _PATIENT_FIELDS}, "admission": {key: before.get(key) for key in _ADMISSION_FIELDS}}
            after_snapshot = {"case": dict(after), "patient": dict(patient), "admission": dict(admission)}
            cursor.execute("INSERT INTO operblock_archive_edit_history (operation_case_id, admission_id, expected_case_revision, expected_admission_revision, before_json, after_json, edited_at, edited_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (int(operation_case_id), int(before["admission_id"]), expected_operation_case_revision, expected_admission_revision, json.dumps(before_snapshot, ensure_ascii=False, default=str, sort_keys=True), json.dumps(after_snapshot, ensure_ascii=False, default=str, sort_keys=True), now, str(self.client_id)))
            return {"operation_case_id": int(operation_case_id), "admission_id": int(before["admission_id"]), "revision": int(after["revision"])}
        return dict(self.db.run_write_operation(operation, source="operblock_update_archived_case"))

    def list_archived_operation_case_edit_history(self, operation_case_id: int) -> list[dict[str, Any]]:
        try:
            rows = self.db.fetch_all_remcard("SELECT * FROM operblock_archive_edit_history WHERE operation_case_id=? ORDER BY id DESC", (int(operation_case_id),))
        except sqlite3.OperationalError:
            return []
        return [dict(row) for row in rows]
