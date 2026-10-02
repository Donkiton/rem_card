from __future__ import annotations

from datetime import datetime, timedelta
import json
import re
import sqlite3
from typing import Any

from rem_card.data.dto.remcard_dto import PatientStatus
from rem_card.services.concurrency import (
    DATA_CONFLICT_MESSAGE,
    DataConflictError,
    assert_revision_matches,
)


HANDOFF_WAITING = "waiting"
HANDOFF_ACCEPTED = "accepted"
HANDOFF_RETURNED_TO_RAO = "returned_to_rao"
HANDOFF_COMPLETED_NON_RAO = "completed_non_rao"
HANDOFF_CANCELLED = "cancelled"


def _handoff_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _table_exists_in_db(db_manager, table_name: str) -> bool:
    try:
        return bool(db_manager.fetch_one_remcard(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
            (table_name,),
        ))
    except Exception:
        return False


class OperBlockLocalRaoHandoffService:
    """Doctor-side actions for invitations created after a local OpBlock export.

    There is intentionally no background queue claim here.  A doctor explicitly
    chooses a free bed, an existing RAO admission, or durable dismissal.
    """

    _TABLE = "operblock_rao_handoff_invitations"
    _WINDOW = timedelta(minutes=30)
    _SNOOZE = timedelta(minutes=15)

    def __init__(self, db_manager):
        self.db = db_manager

    def available(self) -> bool:
        return _table_exists_in_db(self.db, self._TABLE)

    def list_pending(self, *, include_snoozed: bool = False) -> list[dict[str, Any]]:
        if not self.available():
            return []
        where = "status = 'pending'"
        params: tuple[Any, ...] = ()
        if not include_snoozed:
            where += " AND (notification_snoozed_until IS NULL OR DATETIME(notification_snoozed_until) <= DATETIME('now', 'localtime'))"
        # Stale imported cases remain in the OperBlock archive but must never
        # surface as a doctor admission invitation after their clinical window.
        where += " AND DATETIME(transfer_datetime) >= DATETIME('now', 'localtime', '-30 minutes')"
        rows = self.db.fetch_all_remcard(
            f"""
            SELECT * FROM {self._TABLE}
            WHERE {where}
            ORDER BY DATETIME(transfer_datetime) ASC, id ASC
            """,
            params,
        )
        result = []
        for row in rows or []:
            data = dict(row)
            data["payload"] = _json_dict(data.pop("payload_json", None))
            result.append(data)
        return result

    def list_free_beds(self) -> list[int]:
        rows = self.db.fetch_all_remcard(
            """
            SELECT bed_number FROM beds
            WHERE status = 'FREE' AND current_admission_id IS NULL
            ORDER BY CAST(bed_number AS INTEGER), bed_number
            """
        )
        return [int(row["bed_number"]) for row in rows or []]

    def list_active_admissions(self) -> list[dict[str, Any]]:
        rows = self.db.fetch_all_remcard(
            """
            SELECT a.id, a.bed_number, a.history_number, p.full_name
            FROM admissions a
            JOIN patients p ON p.id = a.patient_id
            WHERE COALESCE(a.is_active, 1) = 1
            ORDER BY CAST(a.bed_number AS INTEGER), a.id
            """
        )
        return [dict(row) for row in rows or []]

    def mark_seen(self, invitation_id: int, *, defer: bool = True) -> bool:
        """Persist a closed notification as pending-but-snoozed, never rejected."""
        if not self.available():
            return False
        now = datetime.now().replace(microsecond=0)
        snooze = (now + self._SNOOZE).isoformat(timespec="seconds") if defer else None

        def operation(cursor: sqlite3.Cursor):
            cursor.execute(
                f"""
                UPDATE {self._TABLE}
                SET notification_seen_at = ?, notification_snoozed_until = ?,
                    updated_at = ?, revision = COALESCE(revision, 0) + 1
                WHERE id = ? AND status = 'pending'
                """,
                (now.isoformat(timespec="seconds"), snooze, now.isoformat(timespec="seconds"), int(invitation_id)),
            )
            return bool(cursor.rowcount)
        return bool(self.db.run_write_operation(operation, source="operblock_local_rao_handoff_seen"))

    def dismiss(self, invitation_id: int, *, actor: str = "doctor") -> bool:
        if not self.available():
            return False
        now_text = datetime.now().replace(microsecond=0).isoformat(timespec="seconds")

        def operation(cursor: sqlite3.Cursor):
            cursor.execute(
                f"""
                UPDATE {self._TABLE}
                SET status = 'dismissed', dismissed_at = ?, dismissed_by = ?,
                    notification_snoozed_until = NULL, updated_at = ?,
                    revision = COALESCE(revision, 0) + 1
                WHERE id = ? AND status = 'pending'
                """,
                (now_text, str(actor or "doctor"), now_text, int(invitation_id)),
            )
            return bool(cursor.rowcount)
        return bool(self.db.run_write_operation(operation, source="operblock_local_rao_handoff_dismiss"))

    def accept_to_existing_admission(
        self,
        invitation_id: int,
        admission_id: int,
        *,
        actor: str = "doctor",
    ) -> dict[str, Any]:
        return self._accept(int(invitation_id), existing_admission_id=int(admission_id), actor=actor)

    def accept_to_bed(
        self,
        invitation_id: int,
        bed_number: int,
        *,
        actor: str = "doctor",
    ) -> dict[str, Any]:
        return self._accept(int(invitation_id), bed_number=int(bed_number), actor=actor)

    def _accept(
        self,
        invitation_id: int,
        *,
        actor: str,
        existing_admission_id: int | None = None,
        bed_number: int | None = None,
    ) -> dict[str, Any]:
        if not self.available():
            raise RuntimeError("Приглашение из оперблока ещё не опубликовано в центральной базе.")
        if (existing_admission_id is None) == (bed_number is None):
            raise ValueError("Выберите койку или существующую карту РАО.")

        def operation(cursor: sqlite3.Cursor):
            row = cursor.execute(
                f"SELECT * FROM {self._TABLE} WHERE id = ? LIMIT 1", (int(invitation_id),)
            ).fetchone()
            if not row:
                raise RuntimeError("Приглашение уже недоступно.")
            invitation = dict(row)
            if str(invitation.get("status") or "") != "pending":
                raise RuntimeError("Приглашение уже обработано другим врачом.")
            now = datetime.now().replace(microsecond=0)
            transfer_dt = _handoff_datetime(invitation.get("transfer_datetime"))
            if transfer_dt is None or now - transfer_dt > self._WINDOW or transfer_dt > now + timedelta(minutes=5):
                cursor.execute(
                    f"""
                    UPDATE {self._TABLE}
                    SET status = 'expired', updated_at = ?, revision = COALESCE(revision, 0) + 1
                    WHERE id = ? AND status = 'pending'
                    """,
                    (now.isoformat(timespec="seconds"), int(invitation_id)),
                )
                # Return normally so the write transaction commits the durable
                # expiry; the user-facing error is raised after that commit.
                return {"expired": True}

            accepted_admission_id: int
            accepted_bed_number: int | None = None
            if existing_admission_id is not None:
                target = cursor.execute(
                    "SELECT id FROM admissions WHERE id = ? LIMIT 1", (int(existing_admission_id),)
                ).fetchone()
                if not target:
                    raise RuntimeError("Выбранная карта РАО больше не существует.")
                accepted_admission_id = int(target["id"])
            elif invitation.get("source_rao_admission_id") not in (None, ""):
                # Return to an original RAO card is a link only: do not overwrite a
                # newer movement or claim its bed.
                source = cursor.execute(
                    """
                    SELECT a.id, a.history_number, p.full_name, p.birth_date,
                           pse.status AS current_status
                    FROM admissions a
                    JOIN patients p ON p.id = a.patient_id
                    JOIN patient_status_events pse
                      ON pse.admission_id = a.id AND pse.end_time IS NULL
                    WHERE a.id = ? LIMIT 1
                    """,
                    (int(invitation["source_rao_admission_id"]),),
                ).fetchone()
                if source:
                    original = cursor.execute(
                        """
                        SELECT a.history_number, p.full_name, p.birth_date
                        FROM admissions a JOIN patients p ON p.id = a.patient_id
                        WHERE a.id = ? LIMIT 1
                        """,
                        (int(invitation["remote_admission_id"]),),
                    ).fetchone()
                    same_identity = bool(
                        original
                        and normalize_handoff_history_number(source["history_number"])
                        == normalize_handoff_history_number(original["history_number"])
                        and normalize_handoff_full_name(source["full_name"])
                        == normalize_handoff_full_name(original["full_name"])
                        and str(source["birth_date"] or "") == str(original["birth_date"] or "")
                    )
                    if not same_identity or str(source["current_status"] or "") != PatientStatus.OR.value:
                        raise RuntimeError(
                            "Исходная карта РАО уже изменилась; выберите существующую карту вручную."
                        )
                    accepted_admission_id = int(source["id"])
                else:
                    raise RuntimeError("Исходная карта РАО не найдена; выберите существующую карту вручную.")
            else:
                accepted_admission_id, accepted_bed_number = self._create_admission_on_free_bed(
                    cursor, invitation, int(bed_number)
                )

            now_text = now.isoformat(timespec="seconds")
            cursor.execute(
                f"""
                UPDATE {self._TABLE}
                SET status = 'accepted', accepted_admission_id = ?, accepted_bed_number = ?,
                    accepted_at = ?, accepted_by = ?, notification_snoozed_until = NULL,
                    updated_at = ?, revision = COALESCE(revision, 0) + 1
                WHERE id = ? AND status = 'pending'
                """,
                (accepted_admission_id, accepted_bed_number, now_text, str(actor or "doctor"), now_text, int(invitation_id)),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Приглашение уже обработано другим врачом.")
            return {
                "invitation_id": int(invitation_id),
                "admission_id": accepted_admission_id,
                "bed_number": accepted_bed_number,
            }
        result = dict(self.db.run_write_operation(operation, source="operblock_local_rao_handoff_accept"))
        if result.get("expired"):
            raise RuntimeError("С момента клинического перевода прошло более 30 минут; случай оставлен только в архиве.")
        return result

    @staticmethod
    def _create_admission_on_free_bed(cursor: sqlite3.Cursor, invitation: dict[str, Any], bed_number: int) -> tuple[int, int]:
        bed = cursor.execute(
            "SELECT bed_number FROM beds WHERE bed_number = ? AND status = 'FREE' AND current_admission_id IS NULL",
            (int(bed_number),),
        ).fetchone()
        if not bed:
            raise RuntimeError("Выбранная койка уже занята. Обновите список коек.")
        source = cursor.execute(
            """
            SELECT patient_id, history_number, patient_gender, diagnosis_code, diagnosis_text,
                   department_profile
            FROM admissions WHERE id = ? LIMIT 1
            """,
            (int(invitation["remote_admission_id"]),),
        ).fetchone()
        if not source:
            raise RuntimeError("Импортированная операционная карта не найдена.")
        now_text = datetime.now().replace(microsecond=0).isoformat(timespec="seconds")
        cursor.execute(
            """
            INSERT INTO admissions (
                patient_id, bed_number, history_number, admission_datetime,
                patient_gender, diagnosis_code, diagnosis_text, department_profile,
                source_department, recovery_bed_stay, created_at, updated_at, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Оперблок', 0, ?, ?, 1)
            """,
            (
                int(source["patient_id"]), int(bed_number), str(source["history_number"] or ""),
                str(invitation["transfer_datetime"]), source["patient_gender"],
                source["diagnosis_code"], source["diagnosis_text"], source["department_profile"],
                now_text, now_text,
            ),
        )
        admission_id = int(cursor.lastrowid)
        cursor.execute(
            """
            UPDATE beds SET status = 'OCCUPIED', current_admission_id = ?,
                revision = COALESCE(revision, 0) + 1
            WHERE bed_number = ? AND status = 'FREE' AND current_admission_id IS NULL
            """,
            (admission_id, int(bed_number)),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Выбранная койка уже занята. Обновите список коек.")
        return admission_id, int(bed_number)


def normalize_handoff_history_number(value: Any) -> str:
    text = re.sub(r"\s+", "", str(value or "").strip()).replace("\\", "/")
    return text.upper().replace("Ё", "Е")


def normalize_handoff_full_name(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold().replace("ё", "е")


def _row_dict(row) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        result = json.loads(str(value))
    except Exception:
        return {}
    return dict(result) if isinstance(result, dict) else {}


class OperBlockHandoffService:
    """Coordinates explicit RAO -> operblock handoffs without background polling."""

    def __init__(self, db_manager, *, client_id: str | None = None):
        self.db = db_manager
        self.client_id = str(client_id or "")

    def is_available(self) -> bool:
        row = self.db.fetch_one_remcard(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'operblock_handoffs' LIMIT 1"
        )
        return bool(row)

    @staticmethod
    def _table_exists(cursor: sqlite3.Cursor, table_name: str) -> bool:
        row = cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
            (str(table_name),),
        ).fetchone()
        return bool(row)

    @staticmethod
    def _latest_vitals_snapshot(
        cursor: sqlite3.Cursor,
        admission_id: int,
        dispatched_at: datetime,
    ) -> dict[str, Any]:
        target = dispatched_at.isoformat(timespec="seconds")
        result: dict[str, Any] = {}
        for field in ("sys", "dia", "pulse", "temp", "spo2", "rr", "cvp"):
            row = cursor.execute(
                f"""
                SELECT "{field}" AS value, datetime AS measured_at
                FROM vitals
                WHERE admission_id = ?
                  AND "{field}" IS NOT NULL
                  AND DATETIME(datetime) <= DATETIME(?)
                ORDER BY DATETIME(datetime) DESC, id DESC
                LIMIT 1
                """,
                (int(admission_id), target),
            ).fetchone()
            result[field] = row["value"] if row else None
            result[f"{field}_measured_at"] = row["measured_at"] if row else None
        result["snapshot_at"] = target
        return result

    def dispatch_from_rao(
        self,
        admission_id: int,
        *,
        reason_text: str = "",
        user_id: str | None = None,
        expected_active_event_id: int | None = None,
        expected_active_revision: int | None = None,
    ) -> dict[str, Any]:
        """Atomically records OR movement and a waiting handoff for an occupied RAO bed."""

        dispatched_at = datetime.now().replace(second=0, microsecond=0)
        dispatched_text = dispatched_at.isoformat(timespec="seconds")
        expected_arrival = dispatched_at + timedelta(minutes=5)
        expected_arrival_text = expected_arrival.isoformat(timespec="seconds")
        actor = str(user_id or "USER")

        def operation(cursor: sqlite3.Cursor):
            if not self._table_exists(cursor, "operblock_handoffs"):
                raise RuntimeError("Схема очереди оперблока не установлена.")

            source = cursor.execute(
                """
                SELECT
                    a.id AS admission_id,
                    a.patient_id,
                    a.history_number,
                    a.patient_gender,
                    a.diagnosis_code,
                    a.diagnosis_text,
                    a.department_profile,
                    a.bed_number,
                    a.merged_into_admission_id,
                    p.full_name,
                    p.birth_date,
                    p.last_name,
                    p.first_name,
                    p.middle_name,
                    b.bed_number AS current_bed_number,
                    b.status AS bed_status
                FROM admissions a
                JOIN patients p ON p.id = a.patient_id
                JOIN beds b ON b.current_admission_id = a.id
                WHERE a.id = ?
                LIMIT 1
                """,
                (int(admission_id),),
            ).fetchone()
            if not source:
                raise RuntimeError("Исходная карта не занимает койку РАО.")
            source_data = _row_dict(source)
            if source_data.get("merged_into_admission_id") is not None:
                raise RuntimeError("Слитую карту нельзя отправить в операционную.")
            if str(source_data.get("bed_status") or "").upper() != "OCCUPIED":
                raise RuntimeError("Койка исходной карты не занята.")

            current = cursor.execute(
                """
                SELECT id, status, start_time, COALESCE(revision, 0) AS revision
                FROM patient_status_events
                WHERE admission_id = ? AND end_time IS NULL
                LIMIT 1
                """,
                (int(admission_id),),
            ).fetchone()
            if current:
                if expected_active_event_id is not None and int(current["id"]) != int(expected_active_event_id):
                    raise DataConflictError(DATA_CONFLICT_MESSAGE)
                assert_revision_matches(current["revision"], expected_active_revision)
                if str(current["status"] or "") in {
                    PatientStatus.TRANSFERRED.value,
                    PatientStatus.DEAD.value,
                }:
                    raise RuntimeError("Для карты с финальным исходом движение изменить нельзя.")

            existing = cursor.execute(
                """
                SELECT *
                FROM operblock_handoffs
                WHERE source_admission_id = ?
                  AND status IN ('waiting', 'accepted')
                ORDER BY id DESC
                LIMIT 1
                """,
                (int(admission_id),),
            ).fetchone()
            if existing:
                data = _row_dict(existing)
                data["already_exists"] = True
                return data

            if current and str(current["status"] or "") != PatientStatus.OR.value:
                cursor.execute(
                    """
                    UPDATE patient_status_events
                    SET end_time = ?,
                        updated_at = ?,
                        last_modified_by = ?,
                        revision = COALESCE(revision, 0) + 1
                    WHERE id = ? AND end_time IS NULL
                    """,
                    (dispatched_text, dispatched_text, actor, int(current["id"])),
                )
                if cursor.rowcount != 1:
                    raise DataConflictError(DATA_CONFLICT_MESSAGE)
            if not current or str(current["status"] or "") != PatientStatus.OR.value:
                cursor.execute(
                    """
                    INSERT INTO patient_status_events (
                        admission_id, status, reason_type, reason_text, start_time,
                        created_by, created_at, updated_at, last_modified_by
                    ) VALUES (?, 'OR', 'operblock_handoff', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(admission_id),
                        str(reason_text or "").strip() or "Отправлен в операционную",
                        dispatched_text,
                        actor,
                        dispatched_text,
                        dispatched_text,
                        actor,
                    ),
                )

            patient_snapshot = {
                "patient_id": int(source_data["patient_id"]),
                "admission_id": int(source_data["admission_id"]),
                "history_number": str(source_data.get("history_number") or ""),
                "full_name": str(source_data.get("full_name") or ""),
                "birth_date": source_data.get("birth_date"),
                "gender": source_data.get("patient_gender"),
                "diagnosis_code": source_data.get("diagnosis_code"),
                "diagnosis_text": source_data.get("diagnosis_text"),
                "department_profile": source_data.get("department_profile"),
                "bed_number_at_dispatch": int(source_data["current_bed_number"]),
            }
            vitals_snapshot = self._latest_vitals_snapshot(cursor, int(admission_id), dispatched_at)
            cursor.execute(
                """
                INSERT INTO operblock_handoffs (
                    source_patient_id, source_admission_id, bed_number_at_dispatch,
                    history_number_normalized, dispatched_at, expected_arrival_at,
                    patient_snapshot_json, vitals_snapshot_json, status,
                    created_by, created_by_client_id, last_modified_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'waiting', ?, ?, ?)
                """,
                (
                    int(source_data["patient_id"]),
                    int(admission_id),
                    int(source_data["current_bed_number"]),
                    normalize_handoff_history_number(source_data.get("history_number")),
                    dispatched_text,
                    expected_arrival_text,
                    json.dumps(patient_snapshot, ensure_ascii=False, sort_keys=True, default=str),
                    json.dumps(vitals_snapshot, ensure_ascii=False, sort_keys=True, default=str),
                    actor,
                    self.client_id or None,
                    actor,
                ),
            )
            handoff_id = int(cursor.lastrowid)
            return {
                "id": handoff_id,
                "source_patient_id": int(source_data["patient_id"]),
                "source_admission_id": int(admission_id),
                "bed_number_at_dispatch": int(source_data["current_bed_number"]),
                "dispatched_at": dispatched_text,
                "expected_arrival_at": expected_arrival_text,
                "patient_snapshot": patient_snapshot,
                "vitals_snapshot": vitals_snapshot,
                "status": HANDOFF_WAITING,
                "already_exists": False,
            }

        return dict(self.db.run_write_operation(operation, source="operblock_handoff_dispatch"))

    def list_waiting(self) -> list[dict[str, Any]]:
        """Read the queue only on an explicit user request."""

        rows = self.db.fetch_all_remcard(
            """
            SELECT
                h.*,
                COALESCE(current_bed.bed_number, h.bed_number_at_dispatch) AS current_bed_number
            FROM operblock_handoffs h
            JOIN admissions a ON a.id = h.source_admission_id
            JOIN patient_status_events pse
              ON pse.admission_id = h.source_admission_id
             AND pse.end_time IS NULL
             AND pse.status = 'OR'
            LEFT JOIN beds current_bed
              ON current_bed.current_admission_id = h.source_admission_id
             AND current_bed.status = 'OCCUPIED'
            WHERE h.status = 'waiting'
              AND a.merged_into_admission_id IS NULL
              AND current_bed.current_admission_id IS NOT NULL
            ORDER BY DATETIME(h.dispatched_at) ASC, h.id ASC
            """
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            data = _row_dict(row)
            data["patient_snapshot"] = _json_dict(data.pop("patient_snapshot_json", None))
            data["vitals_snapshot"] = _json_dict(data.pop("vitals_snapshot_json", None))
            result.append(data)
        return result

    def get_waiting(self, handoff_id: int) -> dict[str, Any]:
        row = self.db.fetch_one_remcard(
            """
            SELECT
                h.*,
                COALESCE(current_bed.bed_number, h.bed_number_at_dispatch) AS current_bed_number
            FROM operblock_handoffs h
            LEFT JOIN beds current_bed
              ON current_bed.current_admission_id = h.source_admission_id
             AND current_bed.status = 'OCCUPIED'
            WHERE h.id = ? AND h.status = 'waiting'
            """,
            (int(handoff_id),),
        )
        data = _row_dict(row)
        if not data:
            return {}
        data["patient_snapshot"] = _json_dict(data.pop("patient_snapshot_json", None))
        data["vitals_snapshot"] = _json_dict(data.pop("vitals_snapshot_json", None))
        return data

    def find_waiting_candidates(
        self,
        *,
        history_number: str,
        full_name: str = "",
        birth_date: Any = None,
    ) -> list[dict[str, Any]]:
        normalized_history = normalize_handoff_history_number(history_number)
        if not normalized_history:
            return []
        rows = self.db.fetch_all_remcard(
            """
            SELECT h.*
            FROM operblock_handoffs h
            WHERE h.status = 'waiting'
              AND h.history_number_normalized = ?
            ORDER BY DATETIME(h.dispatched_at) ASC, h.id ASC
            """,
            (normalized_history,),
        )
        normalized_name = normalize_handoff_full_name(full_name)
        birth_text = str(birth_date or "").strip()
        result: list[dict[str, Any]] = []
        for row in rows:
            data = _row_dict(row)
            patient = _json_dict(data.pop("patient_snapshot_json", None))
            data["vitals_snapshot"] = _json_dict(data.pop("vitals_snapshot_json", None))
            data["patient_snapshot"] = patient
            data["full_name_matches"] = bool(
                normalized_name
                and normalize_handoff_full_name(patient.get("full_name")) == normalized_name
            )
            data["birth_date_matches"] = bool(
                birth_text and str(patient.get("birth_date") or "").strip() == birth_text
            )
            result.append(data)
        return result

    @staticmethod
    def mark_cancelled_for_status_change(
        cursor: sqlite3.Cursor,
        admission_id: int,
        *,
        actor: str | None = None,
    ) -> int:
        cursor.execute(
            """
            UPDATE operblock_handoffs
            SET status = 'cancelled',
                last_modified_by = ?,
                revision = COALESCE(revision, 0) + 1
            WHERE source_admission_id = ?
              AND status = 'waiting'
            """,
            (str(actor or "USER"), int(admission_id)),
        )
        return int(cursor.rowcount or 0)
