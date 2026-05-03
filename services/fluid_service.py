from typing import Any, Callable, List, Optional, Tuple
from datetime import datetime, timedelta
from ..data.dto.remcard_dto import FluidDTO
from ..data.dao.fluids_dao import FluidsDAO
from .concurrency import DataConflictError, DATA_CONFLICT_MESSAGE, assert_revision_matches
from .vital_service import VitalService


BALANCE_OUTPUT_FIELDS = {"urine", "drain_output", "ng_output", "stool", "other_output"}
BALANCE_OUTCOME_GRACE = timedelta(hours=1)


class FluidService:
    def __init__(self, fluids_dao: FluidsDAO, vital_service: VitalService, data_service=None):
        self.fluids_dao = fluids_dao
        self.vital_service = vital_service
        self.data_service = data_service

    def get_fluids(self, admission_id: int, date: datetime) -> List[FluidDTO]:
        start, end = self.get_balance_bounds(admission_id, date)
        return self.fluids_dao.get_fluids(admission_id, start, end)

    def enqueue_write(
        self,
        description: str,
        operation: Callable[[], Any],
        on_success: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ):
        if self.data_service:
            self.data_service.enqueue_write(
                description=description,
                operation=operation,
                on_success=on_success,
                on_error=on_error,
            )
            return
        try:
            result = operation()
        except Exception as exc:
            if on_error:
                on_error(exc)
                return
            raise
        if on_success:
            on_success(result)

    def get_balance_bounds(self, admission_id: int, date: datetime) -> Tuple[datetime, datetime]:
        """
        Границы именно для баланса жидкости.

        В UI для баланса уже используется правило "исход + 1 час": после
        перевода/смерти можно закрыть ближайший час выведения. Обычные
        effective_bounds режут данные ровно по времени исхода, из-за чего
        запись 16:00 исчезала при исходе в 15:40.
        """
        shift_start, shift_end = self.vital_service.shift_service.get_day_period(date)
        patient = self.vital_service.patient_dao.get_patient_by_id(admission_id)
        start = max(shift_start, patient.admission_datetime) if patient and patient.admission_datetime else shift_start
        end = shift_end

        terminal_dt = None
        status_service = getattr(self.vital_service, "status_service", None)
        if status_service:
            try:
                status_event = status_service.get_current_status(admission_id)
            except Exception:
                status_event = None
            status_value = getattr(status_event, "status", None)
            if status_event and getattr(status_value, "is_outcome", lambda: False)():
                terminal_dt = status_event.start_time
            elif status_event:
                terminal_dt = None
            elif patient:
                terminal_dt = getattr(patient, "transfer_datetime", None)
        elif patient:
            terminal_dt = getattr(patient, "transfer_datetime", None)

        if terminal_dt:
            end = min(shift_end, terminal_dt + BALANCE_OUTCOME_GRACE)
        return max(start, shift_start), min(end, shift_end)

    def add_fluid(self, dto: FluidDTO, shift_date: Optional[datetime] = None):
        is_ok, msg = self.vital_service.validate_timestamp(dto.admission_id, dto.timestamp, shift_date)
        if not is_ok:
            raise ValueError(msg)
        with self.fluids_dao.db.remcard_transaction():
            self.fluids_dao.add_fluid(dto)

    def upsert_hourly_output(
        self,
        admission_id: int,
        shift_date: datetime,
        hour: int,
        row_key: str,
        value: float,
        is_sum: bool = False,
        expected_revision: Optional[int] = None,
    ):
        """
        Сохраняет выведение по конкретному часу и показателю.
        Возвращает dict с metadata для undo.
        """
        if row_key not in BALANCE_OUTPUT_FIELDS:
            raise ValueError(f"Unsupported fluid output field: {row_key}")

        target_dt = self._resolve_hour_datetime(admission_id, shift_date, hour)
        start_dt, end_dt = self.get_balance_bounds(admission_id, shift_date)
        if target_dt < start_dt or target_dt >= end_dt:
            raise ValueError(
                "Время вне допустимого периода баланса "
                f"({start_dt.strftime('%d.%m %H:%M')} - {end_dt.strftime('%d.%m %H:%M')})"
            )
        hour_key = target_dt.strftime("%Y-%m-%d %H")
        value = float(value)

        with self.fluids_dao.db.remcard_transaction() as cursor:
            cursor.execute(
                f"""
                SELECT id, {row_key} AS current_value, COALESCE(revision, 0) AS revision
                FROM fluids
                WHERE admission_id = ?
                  AND STRFTIME('%Y-%m-%d %H', datetime) = ?
                ORDER BY datetime ASC, id ASC
                LIMIT 1
                """,
                (admission_id, hour_key),
            )
            row = cursor.fetchone()
            if row:
                old_revision = int(row["revision"] or 0)
                assert_revision_matches(old_revision, expected_revision)
                old_value = float(row["current_value"] or 0.0)
                new_value = old_value + value if is_sum else value
                cursor.execute(
                    f"""
                    UPDATE fluids
                    SET {row_key} = ?,
                        last_modified_by = ?,
                        revision = COALESCE(revision, 0) + 1,
                        updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                    WHERE id = ?
                    """,
                    (new_value, "balance", row["id"]),
                )
                if cursor.rowcount != 1:
                    raise DataConflictError(DATA_CONFLICT_MESSAGE)
                return {
                    "action": "update",
                    "fluid_id": row["id"],
                    "old_value": old_value,
                    "new_value": new_value,
                    "old_revision": old_revision,
                    "new_revision": old_revision + 1,
                }

            dto = FluidDTO(
                id=None,
                admission_id=admission_id,
                timestamp=target_dt,
                last_modified_by="balance",
            )
            setattr(dto, row_key, value)
            new_id = self.fluids_dao.add_fluid(dto)
            return {
                "action": "add",
                "fluid_id": new_id,
                "old_value": 0.0,
                "new_value": value,
                "old_revision": None,
                "new_revision": 0,
            }

    def restore_hourly_output(
        self,
        fluid_id: int,
        row_key: str,
        old_value: float,
        expected_revision: Optional[int] = None,
    ):
        if row_key not in BALANCE_OUTPUT_FIELDS:
            raise ValueError(f"Unsupported fluid output field: {row_key}")
        with self.fluids_dao.db.remcard_transaction() as cursor:
            cursor.execute("SELECT COALESCE(revision, 0) AS revision FROM fluids WHERE id = ?", (fluid_id,))
            row = cursor.fetchone()
            if not row:
                raise DataConflictError(DATA_CONFLICT_MESSAGE)
            old_revision = int(row["revision"] or 0)
            assert_revision_matches(old_revision, expected_revision)
            cursor.execute(
                f"""
                UPDATE fluids
                SET {row_key} = ?,
                    last_modified_by = ?,
                    revision = COALESCE(revision, 0) + 1,
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                """,
                (float(old_value), "balance_undo", fluid_id),
            )
            if cursor.rowcount != 1:
                raise DataConflictError(DATA_CONFLICT_MESSAGE)
            return old_revision + 1

    def delete_fluid_by_id(self, fluid_id: int, expected_revision: Optional[int] = None):
        with self.fluids_dao.db.remcard_transaction():
            self.fluids_dao.delete_fluid(fluid_id, expected_revision=expected_revision)

    def get_fluid_row_by_id(self, fluid_id: int):
        return self.fluids_dao.db.fetch_one_remcard("SELECT * FROM fluids WHERE id = ?", (fluid_id,))

    def _resolve_hour_datetime(self, admission_id: int, shift_date: datetime, hour: int) -> datetime:
        dt = shift_date.replace(hour=hour, minute=0, second=0, microsecond=0)
        if hour < 8 and shift_date.hour >= 8:
            dt += timedelta(days=1)

        # Если это час поступления, используем точное время поступления.
        # Важно: нельзя округлять секунды/микросекунды вниз, иначе запись
        # попадет "раньше admission_datetime" и исчезнет из выборки get_fluids(),
        # где effective_start = max(shift_start, admission_datetime).
        patient = self.vital_service.patient_dao.get_patient_by_id(admission_id)
        if patient and patient.admission_datetime:
            adm = patient.admission_datetime
            if dt.date() == adm.date() and dt.hour == adm.hour:
                dt = adm
        return dt
