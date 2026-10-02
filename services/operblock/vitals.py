from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any, Optional
from rem_card.data.dto.remcard_dto import VitalDTO
from rem_card.services.concurrency import DATA_CONFLICT_MESSAGE, DataConflictError, assert_revision_matches
from rem_card.services.vital_validation import validate_vital_dto, validate_vital_values
from .common import (
    OperBlockConflictError,
    validate_operblock_runtime_path,
    _now_text,
    _parse_dt,
    _minute_floor,
    _row_to_dict,
)


class OperBlockVitalsMixin:
    def _list_operation_vitals_for_case(self, case: dict[str, Any]) -> list[VitalDTO]:
        admission_id = int(case["admission_id"])
        started_at = _parse_dt(case.get("started_at"))
        ended_at = _parse_dt(case.get("ended_at"))
        if started_at is None:
            return []
        params: list[Any] = [admission_id, _minute_floor(started_at).isoformat()]
        end_clause = ""
        if ended_at is not None:
            end_clause = 'AND DATETIME("datetime") <= DATETIME(?)'
            params.append(ended_at.isoformat())
        rows = self.db.fetch_all_remcard(
            f"""
            SELECT id, admission_id, datetime, sys, dia, pulse, temp, spo2, rr, cvp,
                   last_modified_by, updated_at, COALESCE(revision, 0) AS revision
            FROM vitals
            WHERE admission_id = ?
              AND DATETIME("datetime") >= DATETIME(?)
              {end_clause}
            ORDER BY DATETIME("datetime") ASC, id ASC
            """,
            tuple(params),
        )
        result = []
        for row in rows:
            data = _row_to_dict(row)
            timestamp = _parse_dt(data.get("datetime"))
            if timestamp is None:
                continue
            result.append(
                VitalDTO(
                    id=data.get("id"),
                    admission_id=data.get("admission_id"),
                    timestamp=timestamp,
                    sys=data.get("sys"),
                    dia=data.get("dia"),
                    pulse=data.get("pulse"),
                    temp=data.get("temp"),
                    spo2=data.get("spo2"),
                    rr=data.get("rr"),
                    cvp=data.get("cvp"),
                    last_modified_by=data.get("last_modified_by"),
                    updated_at=data.get("updated_at"),
                    revision=data.get("revision"),
                )
            )
        return result

    def list_operation_vitals(self, operation_case_id: int) -> list[VitalDTO]:
        case = self._get_case_row(operation_case_id)
        return self._list_operation_vitals_for_case(case)

    def _operation_has_vitals_between(
        self,
        admission_id: int,
        started_at: datetime,
        ended_at: datetime | None = None,
        *,
        cursor: sqlite3.Cursor | None = None,
    ) -> bool:
        params: list[Any] = [int(admission_id), _minute_floor(started_at).isoformat()]
        end_clause = ""
        if ended_at is not None:
            end_clause = 'AND DATETIME("datetime") <= DATETIME(?)'
            params.append(_minute_floor(ended_at).isoformat())
        query = f"""
            SELECT 1
            FROM vitals
            WHERE admission_id = ?
              AND DATETIME("datetime") >= DATETIME(?)
              {end_clause}
            LIMIT 1
        """
        if cursor is not None:
            return bool(cursor.execute(query, tuple(params)).fetchone())
        return bool(self.db.fetch_one_remcard(query, tuple(params)))

    def operation_has_initial_vitals(self, operation_case_id: int) -> bool:
        case = self._get_case_row(operation_case_id)
        started_at = _parse_dt(case.get("started_at"))
        if started_at is None:
            return False
        ended_at = _parse_dt(case.get("ended_at")) if case.get("ended_at") else None
        return self._operation_has_vitals_between(
            int(case["admission_id"]),
            _minute_floor(started_at),
            _minute_floor(ended_at) if ended_at is not None else None,
        )

    def add_vitals(
        self,
        admission_id: int,
        *,
        sys: Optional[int],
        dia: Optional[int],
        pulse: Optional[int],
        spo2: Optional[int],
    ) -> int:
        validate_operblock_runtime_path(self.db)
        validate_vital_values(sys=sys, dia=dia, pulse=pulse, spo2=spo2)
        if sys is None and dia is None and pulse is None and spo2 is None:
            raise ValueError("Введите хотя бы один показатель.")
        now = _now_text()

        def operation(cursor: sqlite3.Cursor):
            case = self._assert_active_operation_for_admission(cursor, admission_id)
            self._assert_datetime_in_operation_bounds(now, case, entity_label="Время витальных функций")
            cursor.execute(
                """
                INSERT INTO vitals (
                    admission_id, datetime, sys, dia, pulse, spo2, last_modified_by, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'operblock', STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
                """,
                (int(admission_id), now, sys, dia, pulse, spo2),
            )
            return int(cursor.lastrowid)

        return int(self.db.run_write_operation(operation, source="operblock_add_vitals"))

    def add_vital_record(self, dto: VitalDTO, *, expected_revision: Optional[int] = None, return_change: bool = False):
        validate_operblock_runtime_path(self.db)
        validate_vital_dto(dto)
        timestamp = getattr(dto, "timestamp", None)
        if not isinstance(timestamp, datetime):
            raise ValueError("Укажите корректное время витальных функций.")
        if (
            getattr(dto, "sys", None) is None
            and getattr(dto, "dia", None) is None
            and getattr(dto, "pulse", None) is None
            and getattr(dto, "temp", None) is None
            and getattr(dto, "spo2", None) is None
            and getattr(dto, "rr", None) is None
            and getattr(dto, "cvp", None) is None
        ):
            raise ValueError("Введите хотя бы один показатель.")

        def operation(cursor: sqlite3.Cursor):
            case = self._assert_active_operation_for_admission(cursor, int(dto.admission_id))
            self._assert_datetime_in_operation_bounds(timestamp, case, entity_label="Время витальных функций")
            target_start = timestamp.replace(second=0, microsecond=0)
            target_end = target_start + timedelta(minutes=1)
            target_start_iso = target_start.isoformat()
            target_end_iso = target_end.isoformat()
            row = cursor.execute(
                """
                SELECT *, COALESCE(revision, 0) AS revision
                FROM vitals
                WHERE admission_id = ?
                  AND DATETIME("datetime") >= DATETIME(?)
                  AND DATETIME("datetime") < DATETIME(?)
                """,
                (int(dto.admission_id), target_start_iso, target_end_iso),
            ).fetchone()
            last_modified_by = dto.last_modified_by or "operblock"
            if row:
                old_revision = int(row["revision"] or 0)
                assert_revision_matches(old_revision, expected_revision)
                revision_clause = ""
                params: list[Any] = [
                    dto.sys,
                    dto.dia,
                    dto.pulse,
                    dto.temp,
                    dto.spo2,
                    dto.rr,
                    dto.cvp,
                    last_modified_by,
                    int(row["id"]),
                ]
                if expected_revision is not None:
                    revision_clause = " AND COALESCE(revision, 0) = ?"
                    params.append(int(expected_revision))
                cursor.execute(
                    f"""
                    UPDATE vitals
                    SET sys = COALESCE(?, sys),
                        dia = COALESCE(?, dia),
                        pulse = COALESCE(?, pulse),
                        temp = COALESCE(?, temp),
                        spo2 = COALESCE(?, spo2),
                        rr = COALESCE(?, rr),
                        cvp = COALESCE(?, cvp),
                        last_modified_by = ?,
                        revision = COALESCE(revision, 0) + 1,
                        updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                    WHERE id = ?
                      {revision_clause}
                    """,
                    tuple(params),
                )
                if cursor.rowcount != 1:
                    raise DataConflictError(DATA_CONFLICT_MESSAGE)
                dto.id = int(row["id"])
                dto.revision = old_revision + 1
                from rem_card.services.vital_undo import vital_change
                return vital_change(dto, row)

            if expected_revision is not None:
                raise DataConflictError(DATA_CONFLICT_MESSAGE)
            cursor.execute(
                """
                INSERT INTO vitals (
                    admission_id, datetime, sys, dia, pulse, temp, spo2, rr, cvp,
                    last_modified_by, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
                """,
                (
                    int(dto.admission_id),
                    timestamp.isoformat(),
                    dto.sys,
                    dto.dia,
                    dto.pulse,
                    dto.temp,
                    dto.spo2,
                    dto.rr,
                    dto.cvp,
                    last_modified_by,
                ),
            )
            dto.id = int(cursor.lastrowid)
            dto.revision = 0
            from rem_card.services.vital_undo import vital_change
            return vital_change(dto, None)

        result = self.db.run_write_operation(operation, source="operblock_add_vital_record")
        # Also hydrate the DTO when an isolated worker confirmed a receipt.
        dto.id, dto.revision = int(result["vital_id"]), int(result["revision"])
        return result if return_change else int(result["vital_id"])

    def undo_vital_change(self, change):
        from rem_card.services.vital_undo import undo_vital_change

        validate_operblock_runtime_path(self.db)

        def operation(cursor):
            case = self._assert_active_operation_for_admission(cursor, int(change["admission_id"]))
            if change.get("operation_case_id") is not None and int(case["operation_case_id"]) != int(change["operation_case_id"]):
                raise OperBlockConflictError("Операционный случай изменился. Обновите протокол.")
            return undo_vital_change(cursor, change)

        return self.db.run_write_operation(operation, source="operblock_undo_vital_change")

    def delete_last_vital_record(
        self,
        admission_id: int,
        *,
        expected_revision: Optional[int] = None,
        expected_vital_id: Optional[int] = None,
    ) -> Optional[int]:
        validate_operblock_runtime_path(self.db)
        if expected_vital_id is None or expected_revision is None:
            raise ValueError("Для отмены нужны идентификатор и версия конкретной записи.")

        def operation(cursor: sqlite3.Cursor):
            case = self._assert_active_operation_for_admission(cursor, admission_id)
            started_at = _parse_dt(case.get("started_at"))
            ended_at = _parse_dt(case.get("ended_at"))
            if started_at is None:
                raise OperBlockConflictError("У операции не задано время начала. Обновите протокол.")
            params: list[Any] = [int(admission_id), int(expected_vital_id), _minute_floor(started_at).isoformat()]
            end_clause = ""
            if ended_at is not None:
                end_clause = 'AND datetime("datetime") <= datetime(?)'
                params.append(_minute_floor(ended_at).isoformat())
            row = cursor.execute(
                f"""
                SELECT id, COALESCE(revision, 0) AS revision
                FROM vitals
                WHERE admission_id = ?
                  AND id = ?
                  AND datetime("datetime") >= datetime(?)
                  {end_clause}
                ORDER BY datetime("datetime") DESC, id DESC
                LIMIT 1
                """,
                tuple(params),
            ).fetchone()
            if not row:
                return None
            assert_revision_matches(row["revision"], expected_revision)
            revision_clause = ""
            delete_params: list[Any] = [int(row["id"])]
            if expected_revision is not None:
                revision_clause = " AND COALESCE(revision, 0) = ?"
                delete_params.append(int(expected_revision))
            cursor.execute(
                f"""
                DELETE FROM vitals
                WHERE id = ?
                  {revision_clause}
                """,
                tuple(delete_params),
            )
            if cursor.rowcount != 1:
                raise DataConflictError(DATA_CONFLICT_MESSAGE)
            return int(row["id"])

        result = self.db.run_write_operation(operation, source="operblock_delete_last_vital_record")
        return int(result) if result is not None else None
