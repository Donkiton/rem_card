import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..data.dao.patient_dao import PatientDAO
from ..data.dao.vitals_dao import VitalsDAO
from ..data.dto.remcard_dto import PatientStatus, VitalDTO
from .shift_service import ShiftService

CHART_LOOKBACK_DAYS = max(0, int(os.environ.get("REMCARD_CHART_LOOKBACK_DAYS", "2")))
CHART_LOOKAHEAD_DAYS = max(0, int(os.environ.get("REMCARD_CHART_LOOKAHEAD_DAYS", "1")))


class VitalService:
    VITAL_SETTINGS_KEYS = ("ad", "pulse", "temp", "spo2", "rr", "cvp")

    def __init__(self, vitals_dao: VitalsDAO, patient_dao: PatientDAO, status_service=None):
        self.vitals_dao = vitals_dao
        self.patient_dao = patient_dao
        self.status_service = status_service
        self.shift_service = ShiftService()
        self._settings_cache = None

    def invalidate_cache(self):
        self._settings_cache = None

    def get_vital_settings_cached(self, admission_id: int, date: datetime) -> Dict[str, Any]:
        if self._settings_cache is None:
            self._settings_cache = {}

        date_str = date.strftime("%Y-%m-%d")
        cache_key = (admission_id, date_str)
        if cache_key in self._settings_cache:
            return self._settings_cache[cache_key].copy()

        settings = self.vitals_dao.get_vital_settings(admission_id, date_str)
        if not settings:
            settings = {"ad": 1, "pulse": 1, "temp": 1, "spo2": 1, "rr": 0, "cvp": 0}

        self._settings_cache[cache_key] = settings
        return settings.copy()

    def save_vital_settings(self, admission_id: int, date: datetime, settings: Dict[str, Any]):
        if self._settings_cache is None:
            self._settings_cache = {}

        date_str = date.strftime("%Y-%m-%d")
        clean_settings = {
            key: int(settings.get(key, 0))
            for key in self.VITAL_SETTINGS_KEYS
            if key in settings
        }
        if not clean_settings:
            clean_settings = {"ad": 1, "pulse": 1, "temp": 1, "spo2": 1, "rr": 0, "cvp": 0}

        dirty_fields_raw = settings.get("__dirty_fields")
        dirty_fields = [
            field for field in dirty_fields_raw if field in self.VITAL_SETTINGS_KEYS
        ] if isinstance(dirty_fields_raw, list) else None

        current_settings = self.vitals_dao.get_vital_settings(admission_id, date_str) or {
            "ad": 1,
            "pulse": 1,
            "temp": 1,
            "spo2": 1,
            "rr": 0,
            "cvp": 0,
        }

        if dirty_fields:
            merged_settings = dict(current_settings)
            for field in dirty_fields:
                merged_settings[field] = clean_settings.get(field, merged_settings.get(field, 0))
        else:
            merged_settings = dict(current_settings)
            merged_settings.update(clean_settings)

        with self.vitals_dao.db.remcard_transaction():
            self.vitals_dao.save_vital_settings(admission_id, date_str, merged_settings)

        self._settings_cache[(admission_id, date_str)] = merged_settings.copy()

    def get_vitals(self, admission_id: int, date: datetime) -> List[VitalDTO]:
        start, end = self.get_effective_bounds(admission_id, date)
        return self.vitals_dao.get_vitals(admission_id, start, end)

    def get_chart_window_bounds(self, date: datetime) -> Tuple[datetime, datetime]:
        s_start, s_end = self.shift_service.get_day_period(date)
        return (
            s_start - timedelta(days=CHART_LOOKBACK_DAYS),
            s_end + timedelta(days=CHART_LOOKAHEAD_DAYS),
        )

    def get_vitals_extended(self, admission_id: int, date: datetime) -> List[VitalDTO]:
        """Return shift vitals with bounded lookback/lookahead context for charts."""
        start, end = self.get_chart_window_bounds(date)
        return self.vitals_dao.get_vitals(
            admission_id,
            start,
            end,
        )

    def add_vital(
        self,
        dto: VitalDTO,
        shift_date: Optional[datetime] = None,
        force: bool = False,
        expected_revision: Optional[int] = None,
    ):
        is_ok, msg = self.validate_timestamp(dto.admission_id, dto.timestamp, shift_date, force)
        if not is_ok:
            raise ValueError(msg)
        with self.vitals_dao.db.remcard_transaction():
            self.vitals_dao.add_vital(dto, expected_revision=expected_revision)

    def clear_vitals(self, admission_id: int, date: datetime):
        start, end = self.shift_service.get_day_period(date)
        with self.vitals_dao.db.remcard_transaction():
            self.vitals_dao.clear_vitals(admission_id, start, end)

    def delete_last_vital(self, admission_id: int, date: datetime, expected_revision: Optional[int] = None):
        start, end = self.get_effective_bounds(admission_id, date)
        vitals = self.vitals_dao.get_vitals(admission_id, start, end)
        if vitals:
            last_vital = vitals[-1]
            with self.vitals_dao.db.remcard_transaction():
                self.vitals_dao.delete_vital(last_vital.id, expected_revision=expected_revision)

    def get_latest_vital_datetime(self, admission_id: int) -> Optional[datetime]:
        return self.vitals_dao.get_latest_vital_datetime(admission_id)

    def get_latest_vital(self, admission_id: int) -> Optional[VitalDTO]:
        return self.vitals_dao.get_latest_vital(admission_id)

    def get_latest_vital_values(self, admission_id: int) -> Dict[str, Any]:
        return self.vitals_dao.get_latest_vital_values(admission_id)

    def get_latest_vital_values_bulk(self, admission_ids: Sequence[int]) -> Dict[int, Dict[str, Any]]:
        result = self.vitals_dao.get_latest_vital_values_bulk(admission_ids)
        default_values = {
            "sys": None,
            "dia": None,
            "pulse": None,
            "temp": None,
            "spo2": None,
            "rr": None,
            "cvp": None,
        }
        for adm_id in admission_ids:
            if adm_id is None:
                continue
            result.setdefault(int(adm_id), dict(default_values))
        return result

    def get_vital_settings_cached_bulk(self, admission_ids: Sequence[int], date: datetime) -> Dict[int, Dict[str, Any]]:
        if self._settings_cache is None:
            self._settings_cache = {}

        date_str = date.strftime("%Y-%m-%d")
        ids = [int(adm_id) for adm_id in admission_ids if adm_id is not None]
        if not ids:
            return {}

        result: Dict[int, Dict[str, Any]] = {}
        missing_ids: List[int] = []
        for adm_id in ids:
            cache_key = (adm_id, date_str)
            cached = self._settings_cache.get(cache_key)
            if cached is not None:
                result[adm_id] = cached.copy()
            else:
                missing_ids.append(adm_id)

        if missing_ids:
            fetched = self.vitals_dao.get_vital_settings_bulk(missing_ids, date_str)
            default_settings = {"ad": 1, "pulse": 1, "temp": 1, "spo2": 1, "rr": 0, "cvp": 0}
            for adm_id in missing_ids:
                row = fetched.get(adm_id) or {}
                merged = {
                    "ad": int(row["ad"]) if row.get("ad") is not None else default_settings["ad"],
                    "pulse": int(row["pulse"]) if row.get("pulse") is not None else default_settings["pulse"],
                    "temp": int(row["temp"]) if row.get("temp") is not None else default_settings["temp"],
                    "spo2": int(row["spo2"]) if row.get("spo2") is not None else default_settings["spo2"],
                    "rr": int(row["rr"]) if row.get("rr") is not None else default_settings["rr"],
                    "cvp": int(row["cvp"]) if row.get("cvp") is not None else default_settings["cvp"],
                }
                self._settings_cache[(adm_id, date_str)] = merged
                result[adm_id] = merged.copy()

        for adm_id in ids:
            result.setdefault(adm_id, {"ad": 1, "pulse": 1, "temp": 1, "spo2": 1, "rr": 0, "cvp": 0})
        return result

    def get_all_vital_dates(self, admission_id: int) -> List[datetime]:
        return self.vitals_dao.get_all_vital_dates(admission_id)

    @staticmethod
    def _minute_floor(value: datetime) -> datetime:
        return value.replace(second=0, microsecond=0)

    def _status_allows_vital_at(self, admission_id: int, timestamp: datetime) -> bool:
        if not self.status_service:
            return True

        event = self.status_service.get_event_at(admission_id, timestamp)
        if event and event.status == PatientStatus.ACTIVE:
            return True

        event_start = getattr(event, "start_time", None)
        if event_start and self._minute_floor(event_start) == self._minute_floor(timestamp):
            return True

        return False

    def validate_timestamp(
        self,
        admission_id: int,
        timestamp: datetime,
        shift_date: Optional[datetime] = None,
        force: bool = False,
    ) -> Tuple[bool, str]:
        patient = self.patient_dao.get_patient_by_id(admission_id)
        if not patient:
            return False, "Пациент не найден"

        timestamp_minute = self._minute_floor(timestamp)

        if shift_date:
            s_start, s_end = self.shift_service.get_day_period(shift_date)
            if timestamp < s_start:
                return False, f"Время раньше начала смены ({s_start.strftime('%H:%M')})"
            if timestamp >= s_end:
                return False, f"Время позже окончания смены ({s_end.strftime('%H:%M')})"

        if patient.admission_datetime and timestamp_minute < self._minute_floor(patient.admission_datetime):
            return False, (
                "Время меньше времени поступления "
                f"({patient.admission_datetime.strftime('%d.%m.%Y %H:%M')})"
            )

        if patient.transfer_datetime and timestamp_minute > self._minute_floor(patient.transfer_datetime):
            return False, (
                "Время больше времени выписки "
                f"({patient.transfer_datetime.strftime('%d.%m.%Y %H:%M')})"
            )

        if self.status_service and not force:
            if not self._status_allows_vital_at(admission_id, timestamp):
                return False, "Пациент вне отделения / в операционной в это время"

        return True, ""

    def get_effective_bounds(self, admission_id: int, shift_date: datetime) -> Tuple[datetime, datetime]:
        s_start, s_end = self.shift_service.get_day_period(shift_date)
        patient = self.patient_dao.get_patient_by_id(admission_id)
        return self.get_effective_bounds_for_patient(patient, shift_date, default_bounds=(s_start, s_end))

    def get_effective_bounds_for_patient(
        self,
        patient,
        shift_date: datetime,
        *,
        default_bounds: Optional[Tuple[datetime, datetime]] = None,
    ) -> Tuple[datetime, datetime]:
        if default_bounds is None:
            s_start, s_end = self.shift_service.get_day_period(shift_date)
        else:
            s_start, s_end = default_bounds
        if not patient:
            return s_start, s_end

        admission_dt = self._minute_floor(patient.admission_datetime) if patient.admission_datetime else None
        transfer_dt = self._minute_floor(patient.transfer_datetime) if patient.transfer_datetime else None
        effective_start = max(s_start, admission_dt) if admission_dt else s_start
        effective_end = min(s_end, transfer_dt) if transfer_dt else s_end
        return effective_start, effective_end
