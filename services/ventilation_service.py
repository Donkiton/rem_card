import json
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from rem_card.app.logger import logger
from ..data.dao.ventilation_dao import VentilationDAO
from ..data.dto.remcard_dto import (
    PatientStatus,
    VentilationCaseDTO,
    VentilationDeliveryType,
    VentilationEventDTO,
    VentilationEventType,
    VentilationMode,
    VentilationStartType,
    VentilationTubeDTO,
)


class VentilationService:
    OUTCOME_SYNC_AUTHOR = "SYSTEM_OUTCOME"
    OUTCOME_REASON_MAP = {
        PatientStatus.TRANSFERRED.value: "переведен",
        PatientStatus.DEAD.value: "умер",
    }

    MODE_FIELDS: dict[VentilationMode, list[str]] = {
        VentilationMode.CONTROLLED_VCV: ["RR", "TV", "PEEP", "FiO2", "Flow"],
        VentilationMode.CONTROLLED_PCV: ["RR", "Pinsp", "PEEP", "FiO2", "Flow"],
        VentilationMode.SIMV_VC: ["RR", "TV", "PEEP", "FiO2", "Flow"],
        VentilationMode.SIMV_PC: ["RR", "Pinsp", "PEEP", "FiO2", "Flow"],
        VentilationMode.PSV: ["PS", "PEEP", "FiO2"],
        VentilationMode.CPAP: ["PEEP", "FiO2"],
        VentilationMode.BIPAP: ["Phigh", "Plow", "Thigh", "Tlow", "FiO2"],
        VentilationMode.SPONTANEOUS: ["FiO2", "Flow"],
    }

    PARAMETER_KEYS = {
        "RR",
        "TV",
        "Pinsp",
        "PEEP",
        "FiO2",
        "PS",
        "Flow",
        "Phigh",
        "Plow",
        "Thigh",
        "Tlow",
    }

    def __init__(self, dao: VentilationDAO, data_service=None):
        self.dao = dao
        self.data_service = data_service

    @classmethod
    def get_mode_fields(cls, mode: str | VentilationMode) -> list[str]:
        mode_enum = mode if isinstance(mode, VentilationMode) else VentilationMode(str(mode).strip().upper())
        return list(cls.MODE_FIELDS.get(mode_enum, []))

    def create_case(
        self,
        admission_id: int,
        *,
        start_time: Optional[datetime] = None,
        start_type: str | VentilationStartType = VentilationStartType.IN_DEPARTMENT,
        delivery_type: str | VentilationDeliveryType = VentilationDeliveryType.UNKNOWN,
        author: Optional[str] = "Доктор",
        initial_mode: Optional[str | VentilationMode] = None,
        initial_parameters: Optional[dict[str, Any]] = None,
        initial_indications: Optional[str] = None,
        o2_flow: Optional[float] = None,
        tube_location: Optional[str] = None,
    ) -> VentilationCaseDTO:
        admission_dt = self.dao.get_admission_datetime(admission_id)
        if not admission_dt:
            raise ValueError("Не найдено время поступления пациента.")

        start_type_enum = self._to_start_type(start_type)
        delivery_type_enum = self._to_delivery_type(delivery_type)
        latest_case = self.dao.get_latest_case(admission_id)
        if latest_case and start_type_enum == VentilationStartType.ADMISSION:
            raise ValueError("Тип начала 'С поступления' доступен только для первого случая ИВЛ.")

        if start_type_enum == VentilationStartType.ADMISSION:
            effective_start = admission_dt
        else:
            effective_start = start_time or datetime.now()
            if effective_start < admission_dt:
                raise ValueError("Начало ИВЛ в отделении не может быть раньше времени поступления.")

        if latest_case and latest_case.end_time:
            min_next_start = latest_case.end_time + timedelta(minutes=1)
            if effective_start < min_next_start:
                raise ValueError(
                    "Начало нового случая ИВЛ не может быть раньше завершения предыдущего. "
                    f"Минимум: {min_next_start.strftime('%d.%m.%Y %H:%M')}"
                )

        if self.dao.get_active_case(admission_id):
            raise ValueError("У пациента уже есть активный случай ИВЛ.")

        if not initial_mode:
            raise ValueError("Для открытия случая ИВЛ необходимо выбрать стартовый режим.")
        if o2_flow is not None:
            raise ValueError("Поток O2 задается только при экстубации.")

        mode_enum = self._to_mode(initial_mode) if initial_mode else None
        normalized_parameters = self._normalize_parameters(mode_enum, initial_parameters or {})
        self._validate_event_payload(
            VentilationEventType.START_VENT,
            mode_enum,
            normalized_parameters,
            extubation_reason=initial_indications,
            o2_flow=None,
        )

        def operation(cursor):
            cursor.execute(
                """
                SELECT 1
                FROM ivl_episodes
                WHERE admission_id = ?
                  AND (is_active = 1 OR (is_active IS NULL AND end_time IS NULL))
                LIMIT 1
                """,
                (admission_id,),
            )
            if cursor.fetchone():
                raise ValueError("У пациента уже есть активный случай ИВЛ.")
            case_id = self.dao.insert_case(
                cursor,
                admission_id=admission_id,
                episode_number=self.dao.get_next_episode_number(admission_id),
                start_time=effective_start,
                case_type="delivery" if start_type_enum == VentilationStartType.ADMISSION else "transfer",
                start_type=start_type_enum,
                delivery_type=delivery_type_enum,
            )
            self.dao.insert_tube(
                cursor,
                admission_id=admission_id,
                case_id=case_id,
                insertion_time=effective_start,
                device_type="ENDOTRACHEAL_TUBE",
                location=tube_location,
            )
            event_id = self.dao.insert_event(
                cursor,
                admission_id=admission_id,
                case_id=case_id,
                event_time=effective_start,
                event_type=VentilationEventType.START_VENT,
                mode=mode_enum,
                parameters_json=self._dump_parameters(normalized_parameters),
                extubation_reason=initial_indications,
                o2_flow=o2_flow,
                author=author,
            )
            if mode_enum or normalized_parameters:
                self.dao.insert_respiratory_support(
                    cursor,
                    admission_id=admission_id,
                    case_id=case_id,
                    event_id=event_id,
                    event_time=effective_start,
                    mode=mode_enum,
                    parameters=normalized_parameters,
                    parameters_json=self._dump_parameters(normalized_parameters),
                )
            return case_id

        case_id = self._run_write(f"vent_create_case:{admission_id}", operation)
        case = self.dao.get_case_by_id(int(case_id))
        if not case:
            raise RuntimeError("Случай ИВЛ создан, но не найден при повторном чтении.")
        return case

    def add_event(
        self,
        case_id: int,
        *,
        event_time: datetime,
        event_type: str | VentilationEventType,
        mode: Optional[str | VentilationMode] = None,
        parameters: Optional[dict[str, Any]] = None,
        extubation_reason: Optional[str] = None,
        o2_flow: Optional[float] = None,
        author: Optional[str] = "Доктор",
        tube_device_type: str = "ENDOTRACHEAL_TUBE",
        tube_location: Optional[str] = None,
        expected_case_revision: Optional[int] = None,
    ) -> VentilationEventDTO:
        case = self.dao.get_case_by_id(case_id)
        if not case:
            raise ValueError("Случай ИВЛ не найден.")

        event_type_enum = self._to_event_type(event_type)
        mode_enum = self._to_mode(mode) if mode else None
        normalized_parameters = self._normalize_parameters(mode_enum, parameters or {})

        self._validate_event_time(case, event_time)
        self._validate_event_sequence(case_id, event_time)
        self._validate_event_payload(
            event_type_enum,
            mode_enum,
            normalized_parameters,
            extubation_reason=extubation_reason,
            o2_flow=o2_flow,
        )

        if event_type_enum == VentilationEventType.EXTUBATION and event_time < case.start_time:
            raise ValueError("Нельзя экстубировать раньше интубации.")
        if not case.is_active:
            raise ValueError("Нельзя добавлять события в закрытый случай ИВЛ.")

        def operation(cursor):
            self.dao.assert_case_revision(cursor, case_id, expected_case_revision)
            event_id = self.dao.insert_event(
                cursor,
                admission_id=case.admission_id,
                case_id=case_id,
                event_time=event_time,
                event_type=event_type_enum,
                mode=mode_enum,
                parameters_json=self._dump_parameters(normalized_parameters),
                extubation_reason=extubation_reason,
                o2_flow=o2_flow,
                author=author,
            )

            if mode_enum or normalized_parameters:
                self.dao.insert_respiratory_support(
                    cursor,
                    admission_id=case.admission_id,
                    case_id=case_id,
                    event_id=event_id,
                    event_time=event_time,
                    mode=mode_enum,
                    parameters=normalized_parameters,
                    parameters_json=self._dump_parameters(normalized_parameters),
                )

            if event_type_enum == VentilationEventType.EXTUBATION:
                self.dao.close_case(cursor, case_id, event_time)
                self.dao.close_active_tubes_for_case(cursor, case_id, event_time)
            elif event_type_enum == VentilationEventType.TRACHEOSTOMY:
                active_tube = self.dao.get_active_tube(case_id)
                if active_tube:
                    self.dao.close_tube(cursor, active_tube.id, event_time)
                self.dao.insert_tube(
                    cursor,
                    admission_id=case.admission_id,
                    case_id=case_id,
                    insertion_time=event_time,
                    device_type="TRACHEOSTOMY_TUBE",
                    location=tube_location,
                )
            elif event_type_enum == VentilationEventType.TUBE_REPLACEMENT:
                active_tube = self.dao.get_active_tube(case_id)
                if active_tube:
                    self.dao.close_tube(cursor, active_tube.id, event_time)
                self.dao.insert_tube(
                    cursor,
                    admission_id=case.admission_id,
                    case_id=case_id,
                    insertion_time=event_time,
                    device_type=tube_device_type or "ENDOTRACHEAL_TUBE",
                    location=tube_location,
                )
            else:
                self.dao.bump_case_revision(cursor, case_id)

            return event_id

        event_id = self._run_write(f"vent_add_event:{case_id}:{event_type_enum.value}", operation)
        events = self.dao.get_case_events(case_id)
        for item in events:
            if item.id == event_id:
                return item
        raise RuntimeError("Событие ИВЛ создано, но не найдено при повторном чтении.")

    def close_case(
        self,
        case_id: int,
        *,
        end_time: Optional[datetime] = None,
        extubation_reason: Optional[str] = None,
        o2_flow: Optional[float] = None,
        author: Optional[str] = "Доктор",
        expected_case_revision: Optional[int] = None,
    ) -> VentilationEventDTO:
        return self.add_event(
            case_id,
            event_time=end_time or datetime.now(),
            event_type=VentilationEventType.EXTUBATION,
            mode=None,
            parameters=None,
            extubation_reason=extubation_reason,
            o2_flow=o2_flow,
            author=author,
            expected_case_revision=expected_case_revision,
        )

    def replace_tube(
        self,
        case_id: int,
        *,
        replacement_time: Optional[datetime] = None,
        author: Optional[str] = "Доктор",
        new_device_type: str = "ENDOTRACHEAL_TUBE",
        location: Optional[str] = None,
        expected_case_revision: Optional[int] = None,
    ) -> VentilationEventDTO:
        return self.add_event(
            case_id,
            event_time=replacement_time or datetime.now(),
            event_type=VentilationEventType.TUBE_REPLACEMENT,
            author=author,
            tube_device_type=new_device_type,
            tube_location=location,
            expected_case_revision=expected_case_revision,
        )

    def get_active_case(self, admission_id: int) -> Optional[VentilationCaseDTO]:
        return self.dao.get_active_case(admission_id)

    def get_latest_case(self, admission_id: int) -> Optional[VentilationCaseDTO]:
        return self.dao.get_latest_case(admission_id)

    def get_case_events(self, case_id: int) -> list[VentilationEventDTO]:
        return self.dao.get_case_events(case_id)

    def get_admission_events(self, admission_id: int) -> list[VentilationEventDTO]:
        events: list[VentilationEventDTO] = []
        for case in self.dao.get_cases(admission_id):
            events.extend(self.dao.get_case_events(case.id))
        events.sort(key=lambda e: (e.timestamp, e.id or 0))
        return events

    def get_case_tubes(self, case_id: int) -> list[VentilationTubeDTO]:
        return self.dao.get_case_tubes(case_id)

    def get_case_duration(self, case_id: int, now: Optional[datetime] = None) -> float:
        case = self.dao.get_case_by_id(case_id)
        if not case:
            return 0.0
        end_time = case.end_time or (now or datetime.now())
        return max(0.0, (end_time - case.start_time).total_seconds())

    def get_total_vent_time(self, admission_id: int, now: Optional[datetime] = None) -> float:
        total = 0.0
        ref_now = now or datetime.now()
        for case in self.dao.get_cases(admission_id):
            end_time = case.end_time or ref_now
            total += max(0.0, (end_time - case.start_time).total_seconds())
        return total

    def get_tube_duration(self, case_id: int, now: Optional[datetime] = None) -> float:
        active_tube = self.dao.get_active_tube(case_id)
        if not active_tube:
            return 0.0
        end_time = active_tube.removal_time or (now or datetime.now())
        return max(0.0, (end_time - active_tube.insertion_time).total_seconds())

    def get_active_case_summary(self, admission_id: int, now: Optional[datetime] = None) -> dict[str, Any]:
        ref_now = now or datetime.now()
        active_case = self.get_active_case(admission_id)
        total_seconds = self.get_total_vent_time(admission_id, ref_now)

        if not active_case:
            return {
                "active_case": None,
                "case_duration_seconds": 0.0,
                "total_duration_seconds": total_seconds,
                "tube_duration_seconds": 0.0,
                "tube_alert": False,
            }

        case_duration = self.get_case_duration(active_case.id, ref_now)
        tube_duration = self.get_tube_duration(active_case.id, ref_now)
        return {
            "active_case": active_case,
            "case_duration_seconds": case_duration,
            "total_duration_seconds": total_seconds,
            "tube_duration_seconds": tube_duration,
            "tube_alert": tube_duration > 72 * 3600,
        }

    def sync_outcome_status(
        self,
        admission_id: int,
        *,
        status_value: Optional[str],
        status_time: Optional[datetime],
    ):
        status_key = str(status_value or "").upper()
        reason = self.OUTCOME_REASON_MAP.get(status_key)
        if not reason or status_time is None:
            self._revert_outcome_autoclose_if_needed(admission_id)
            return

        active_case = self.get_active_case(admission_id)
        if active_case:
            effective_time = max(status_time, active_case.start_time)
            self.close_case(
                active_case.id,
                end_time=effective_time,
                extubation_reason=reason,
                o2_flow=None,
                author=self.OUTCOME_SYNC_AUTHOR,
            )
            return

        latest_case = self.get_latest_case(admission_id)
        if not latest_case or latest_case.id is None:
            return

        auto_ext_event = self.dao.get_outcome_extubation_event(latest_case.id, author=self.OUTCOME_SYNC_AUTHOR)
        if not auto_ext_event or auto_ext_event.id is None:
            return

        effective_time = max(status_time, latest_case.start_time)

        def operation(cursor):
            self.dao.update_extubation_event(
                cursor,
                event_id=int(auto_ext_event.id),
                event_time=effective_time,
                extubation_reason=reason,
                author=self.OUTCOME_SYNC_AUTHOR,
            )
            self.dao.set_case_end_time(cursor, int(latest_case.id), effective_time)
            self.dao.move_tube_closure_time(
                cursor,
                int(latest_case.id),
                from_time=auto_ext_event.timestamp,
                to_time=effective_time,
            )

        self._run_write(f"vent_sync_outcome_update:{admission_id}", operation)

    def _revert_outcome_autoclose_if_needed(self, admission_id: int):
        if self.get_active_case(admission_id):
            return
        latest_case = self.get_latest_case(admission_id)
        if not latest_case or latest_case.id is None:
            return

        auto_ext_event = self.dao.get_outcome_extubation_event(latest_case.id, author=self.OUTCOME_SYNC_AUTHOR)
        if not auto_ext_event or auto_ext_event.id is None:
            return

        last_event = self.dao.get_last_event(latest_case.id)
        if not last_event or last_event.id != auto_ext_event.id:
            return

        self.rollback_last_action(latest_case.id)

    def rollback_last_action(
        self,
        case_id: int,
        *,
        expected_case_revision: Optional[int] = None,
        expected_last_event_revision: Optional[int] = None,
    ) -> VentilationEventDTO:
        case = self.dao.get_case_by_id(case_id)
        if not case:
            raise ValueError("Случай ИВЛ не найден.")

        last_event = self.dao.get_last_event(case_id)
        if not last_event:
            raise ValueError("В этом случае нет событий для отмены.")
        if last_event.id is None:
            raise ValueError("Некорректная запись события ИВЛ: отсутствует идентификатор.")

        def operation(cursor):
            self.dao.assert_case_revision(cursor, case_id, expected_case_revision)
            self.dao.assert_event_revision(cursor, int(last_event.id), expected_last_event_revision)
            self.dao.delete_respiratory_support_by_event(cursor, int(last_event.id))

            if last_event.event_type == VentilationEventType.EXTUBATION:
                self.dao.reopen_case(cursor, case_id)
                self.dao.reopen_tubes_closed_at(cursor, case_id, last_event.timestamp)
            elif last_event.event_type in (
                VentilationEventType.TRACHEOSTOMY,
                VentilationEventType.TUBE_REPLACEMENT,
            ):
                latest_tube_id = self.dao.get_latest_tube_id(cursor, case_id)
                if latest_tube_id is not None:
                    self.dao.delete_tube(cursor, latest_tube_id)
                self.dao.reopen_tubes_closed_at(cursor, case_id, last_event.timestamp)

            self.dao.delete_event(cursor, int(last_event.id))
            remaining_events = self.dao.count_case_events(cursor, case_id)
            if remaining_events == 0:
                self.dao.delete_case_tubes(cursor, case_id)
                self.dao.delete_case(cursor, case_id)
            elif last_event.event_type != VentilationEventType.EXTUBATION:
                self.dao.bump_case_revision(cursor, case_id)

        self._run_write(f"vent_rollback_last_action:{case_id}", operation)
        return last_event

    def _validate_event_time(self, case: VentilationCaseDTO, event_time: datetime):
        if event_time < case.start_time:
            raise ValueError("Время события не может быть раньше начала случая ИВЛ.")
        if case.end_time and event_time > case.end_time:
            raise ValueError("Время события не может быть позже времени закрытия случая ИВЛ.")

    def _validate_event_sequence(self, case_id: int, event_time: datetime):
        previous = self.dao.get_last_event(case_id)
        if previous and event_time < previous.timestamp:
            raise ValueError("Нельзя добавлять события назад во времени.")

    def _validate_event_payload(
        self,
        event_type: VentilationEventType,
        mode: Optional[VentilationMode],
        parameters: dict[str, Any],
        *,
        extubation_reason: Optional[str] = None,
        o2_flow: Optional[float] = None,
    ):
        mode_events = (VentilationEventType.START_VENT, VentilationEventType.MODE_CHANGE)
        no_mode_events = (
            VentilationEventType.EXTUBATION,
            VentilationEventType.TRACHEOSTOMY,
            VentilationEventType.TUBE_REPLACEMENT,
        )

        if event_type in mode_events and mode is None:
            raise ValueError("Для старта ИВЛ и смены режима нужно указать режим.")

        if event_type in no_mode_events and mode is not None:
            raise ValueError("Для выбранного типа события режим ИВЛ указывать нельзя.")

        if parameters and mode is None:
            raise ValueError("Параметры ИВЛ нельзя сохранить без режима.")

        if event_type in no_mode_events and parameters:
            raise ValueError("Для выбранного типа события нельзя сохранять параметры режима.")

        if event_type not in (
            VentilationEventType.START_VENT,
            VentilationEventType.MODE_CHANGE,
            VentilationEventType.EXTUBATION,
            VentilationEventType.TRACHEOSTOMY,
        ) and extubation_reason:
            raise ValueError("Поле 'Показания' доступно только для старта ИВЛ, смены режима, экстубации и трахеостомии.")

        if event_type != VentilationEventType.EXTUBATION and o2_flow is not None:
            raise ValueError("Поток O2 задается только при экстубации.")

    def _normalize_parameters(
        self,
        mode: Optional[VentilationMode],
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        if not parameters:
            return {}

        if mode is None:
            raise ValueError("Параметры ИВЛ нельзя сохранить без режима.")

        normalized: dict[str, Any] = {}
        for raw_key, raw_value in dict(parameters).items():
            key = str(raw_key).strip()
            if not key:
                continue
            canonical = self._normalize_parameter_key(key)
            if canonical not in self.PARAMETER_KEYS:
                raise ValueError(f"Недопустимый параметр ИВЛ: {key}")
            if raw_value is None or str(raw_value).strip() == "":
                continue
            try:
                value: Any = float(raw_value)
                if canonical == "RR":
                    value = int(round(value))
            except Exception as exc:
                raise ValueError(f"Неверное числовое значение параметра {canonical}") from exc
            normalized[canonical] = value

        allowed = set(self.get_mode_fields(mode))
        extras = sorted(set(normalized) - allowed)
        if extras:
            raise ValueError(
                f"Для режима {mode.value} нельзя сохранять параметры: {', '.join(extras)}"
            )
        return normalized

    @staticmethod
    def _normalize_parameter_key(key: str) -> str:
        lut = {
            "fio2": "FiO2",
            "peep": "PEEP",
            "rr": "RR",
            "tv": "TV",
            "pinsp": "Pinsp",
            "ps": "PS",
            "flow": "Flow",
            "phigh": "Phigh",
            "plow": "Plow",
            "thigh": "Thigh",
            "tlow": "Tlow",
        }
        return lut.get(key.lower(), key)

    @staticmethod
    def _dump_parameters(parameters: dict[str, Any]) -> Optional[str]:
        if not parameters:
            return None
        return json.dumps(parameters, ensure_ascii=True, separators=(",", ":"))

    @staticmethod
    def _to_start_type(value: str | VentilationStartType) -> VentilationStartType:
        if isinstance(value, VentilationStartType):
            return value
        return VentilationStartType(str(value).strip().upper())

    @staticmethod
    def _to_delivery_type(value: str | VentilationDeliveryType) -> VentilationDeliveryType:
        if isinstance(value, VentilationDeliveryType):
            return value
        return VentilationDeliveryType(str(value).strip().upper())

    @staticmethod
    def _to_event_type(value: str | VentilationEventType) -> VentilationEventType:
        if isinstance(value, VentilationEventType):
            return value
        return VentilationEventType(str(value).strip().upper())

    @staticmethod
    def _to_mode(value: str | VentilationMode) -> VentilationMode:
        if isinstance(value, VentilationMode):
            return value
        return VentilationMode(str(value).strip().upper())

    def _run_write(self, description: str, operation: Callable):
        if self.data_service:
            return self.data_service.run_write(description, operation)
        return self.dao.db.run_write_operation(operation, source=description)

    def log_mapping_hint(self):
        logger.debug(
            "Ventilation mapping: patient->admissions.patient_id, admission_time->admissions.admission_datetime, "
            "case->ivl_episodes, event->clinical_events, tube->devices, params->respiratory_support.parameters_json"
        )
