import json
import os
import tempfile
from datetime import datetime
from typing import Any, List, Optional

from rem_card.app.paths import SEED_DIR, USER_DICT_DIR
from rem_card.data.dao.diet_dao import DietPlanDAO, OralIntakeDAO
from rem_card.data.dao.exceptions import OptimisticLockError
from rem_card.data.dto.remcard_dto import DietPlanDTO, DietTemplateDTO, OralIntakeEventDTO
from rem_card.services.shift_service import ShiftService


def _dt_to_db(value: datetime) -> str:
    return value.replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")


def normalize_minute(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("datetime expected")
    return value.replace(second=0, microsecond=0)


def normalize_schedule(schedule: Any) -> str:
    if schedule is None or schedule == "":
        raw_items = []
    elif isinstance(schedule, str):
        raw_items = json.loads(schedule)
    else:
        raw_items = schedule

    if not isinstance(raw_items, list):
        raise ValueError("Расписание питания должно быть списком")

    normalized = []
    seen_times = set()
    for item in raw_items:
        if not isinstance(item, dict):
            raise ValueError("Строка расписания питания должна быть объектом")
        time_text = str(item.get("time") or "").strip()
        if not ShiftService.is_time_input_valid(time_text):
            raise ValueError("Время питания должно быть в формате HH:mm")
        normalized_time = ShiftService.normalize_time(time_text)
        if normalized_time in seen_times:
            raise ValueError("В расписании питания не должно быть повторяющихся времен")
        seen_times.add(normalized_time)
        amount = int(float(item.get("amount") or 0))
        if amount <= 0:
            raise ValueError("Объем питания должен быть больше 0 мл")
        normalized.append({"time": normalized_time, "amount": amount})

    normalized.sort(key=lambda item: ((int(item["time"][:2]) - 8) % 24, int(item["time"][3:5])))
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def schedule_items(schedule_json: str) -> list[dict[str, int | str]]:
    try:
        items = json.loads(schedule_json or "[]")
    except Exception:
        return []
    if not isinstance(items, list):
        return []
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        time_text = str(item.get("time") or "").strip()
        if not ShiftService.is_time_input_valid(time_text):
            continue
        try:
            amount = int(float(item.get("amount") or 0))
        except Exception:
            continue
        if amount <= 0:
            continue
        result.append({"time": ShiftService.normalize_time(time_text), "amount": amount})
    result.sort(key=lambda item: ((int(str(item["time"])[:2]) - 8) % 24, int(str(item["time"])[3:5])))
    return result


DIET_TEMPLATES_FILE_NAME = "diet_templates.json"


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _as_bool_int(value: Any) -> int:
    if isinstance(value, str):
        return 1 if value.strip().lower() in ("1", "true", "yes", "y", "да") else 0
    return 1 if bool(value) else 0


class DietTemplateFileStore:
    def __init__(self, path: Optional[str] = None, seed_path: Optional[str] = None):
        self.path = path or os.path.join(USER_DICT_DIR, DIET_TEMPLATES_FILE_NAME)
        self.seed_path = seed_path
        if self.seed_path is None and path is None:
            self.seed_path = os.path.join(SEED_DIR, DIET_TEMPLATES_FILE_NAME)

    def exists(self) -> bool:
        return os.path.exists(self.path)

    def load(self) -> tuple[dict[str, Any], List[DietTemplateDTO]]:
        payload = self._read_payload()
        return payload, self._templates_from_payload(payload)

    def list_templates(self) -> List[DietTemplateDTO]:
        _, templates = self.load()
        return templates

    def save_templates(self, templates: List[DietTemplateDTO], *, next_id: Optional[int] = None):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        ordered = self._sort_templates(templates)
        max_id = max((int(t.id or 0) for t in ordered), default=0)
        payload = {
            "next_id": int(next_id if next_id is not None else max_id + 1),
            "templates": [self._dto_to_json(t) for t in ordered],
        }
        directory = os.path.dirname(self.path)
        fd, tmp_path = tempfile.mkstemp(prefix=".diet_templates_", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.replace(tmp_path, self.path)
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

    def initialize_from_seed(self):
        if self.exists():
            self.list_templates()
            return

        templates: List[DietTemplateDTO] = []
        next_id = 1
        if self.seed_path and os.path.abspath(self.seed_path) != os.path.abspath(self.path) and os.path.exists(self.seed_path):
            payload = self._read_payload(self.seed_path)
            templates = self._templates_from_payload(payload)
            next_id = self.next_id(payload, templates)

        self.save_templates(templates, next_id=next_id)

    def next_id(self, payload: dict[str, Any], templates: List[DietTemplateDTO]) -> int:
        max_id = max((int(t.id or 0) for t in templates), default=0)
        try:
            configured_next = int(payload.get("next_id") or 0)
        except Exception:
            configured_next = 0
        return max(1, max_id + 1, configured_next)

    def _read_payload(self, path: Optional[str] = None) -> dict[str, Any]:
        source_path = path or self.path
        if not os.path.exists(source_path):
            return {"next_id": 1, "templates": []}
        try:
            with open(source_path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Файл шаблонов питания поврежден: {source_path} ({exc})") from exc

        if isinstance(payload, list):
            return {"templates": payload}
        if not isinstance(payload, dict):
            raise ValueError(f"Файл шаблонов питания должен быть JSON-объектом: {source_path}")
        return payload

    def _templates_from_payload(self, payload: dict[str, Any]) -> List[DietTemplateDTO]:
        raw_templates = payload.get("templates", [])
        items: list[tuple[Any, dict[str, Any]]] = []
        if isinstance(raw_templates, dict):
            for key, item in raw_templates.items():
                if isinstance(item, dict):
                    items.append((key, dict(item)))
        elif isinstance(raw_templates, list):
            for index, item in enumerate(raw_templates, start=1):
                if isinstance(item, dict):
                    items.append((index, dict(item)))
        else:
            raise ValueError("Поле templates в diet_templates.json должно быть списком или объектом")

        templates: List[DietTemplateDTO] = []
        used_ids: set[int] = set()
        now = _now_text()
        for fallback_id, raw in items:
            if raw.get("_deleted"):
                continue
            template_id = self._coerce_id(raw.get("id", fallback_id), used_ids)
            used_ids.add(template_id)
            name = str(raw.get("name") or "").strip()
            if not name:
                raise ValueError(f"В шаблоне питания id={template_id} не указано название")
            schedule_source = raw.get("schedule", raw.get("schedule_json", []))
            templates.append(
                DietTemplateDTO(
                    id=template_id,
                    name=name,
                    diet_text=str(raw.get("diet_text") or raw.get("description") or ""),
                    schedule_json=normalize_schedule(schedule_source),
                    is_default=_as_bool_int(raw.get("is_default", raw.get("default", False))),
                    version=self._coerce_int(raw.get("version"), default=1),
                    created_at=str(raw.get("created_at") or now),
                    updated_at=str(raw.get("updated_at") or now),
                    last_modified_by=str(raw.get("last_modified_by") or "doctor"),
                )
            )
        return self._sort_templates(templates)

    @staticmethod
    def _coerce_id(value: Any, used_ids: set[int]) -> int:
        try:
            template_id = int(value)
        except Exception:
            template_id = 0
        if template_id <= 0 or template_id in used_ids:
            template_id = max(used_ids or {0}) + 1
        return template_id

    @staticmethod
    def _coerce_int(value: Any, *, default: int) -> int:
        try:
            result = int(value)
        except Exception:
            result = int(default)
        return max(1, result)

    @staticmethod
    def _sort_templates(templates: List[DietTemplateDTO]) -> List[DietTemplateDTO]:
        return sorted(
            templates,
            key=lambda item: (
                -int(item.is_default or 0),
                str(item.name or "").lower(),
                int(item.id or 0),
            ),
        )

    @staticmethod
    def _dto_to_json(template: DietTemplateDTO) -> dict[str, Any]:
        return {
            "id": int(template.id or 0),
            "name": template.name or "",
            "diet_text": template.diet_text or "",
            "schedule": schedule_items(template.schedule_json),
            "is_default": bool(template.is_default),
            "version": int(template.version or 1),
            "created_at": template.created_at or _now_text(),
            "updated_at": template.updated_at or _now_text(),
            "last_modified_by": template.last_modified_by or "doctor",
        }


class DietTemplateService:
    def __init__(self, file_store: Optional[DietTemplateFileStore] = None):
        self.file_store = file_store or DietTemplateFileStore()
        self._ensure_file_initialized()

    def list_templates(self) -> List[DietTemplateDTO]:
        return self.file_store.list_templates()

    def get_template(self, template_id: int) -> DietTemplateDTO:
        template = self._find_template(template_id)
        if not template:
            raise ValueError("Шаблон питания не найден")
        return template

    def create_template(self, name: str, diet_text: str = "", schedule_json: Any = None, is_default: bool = False):
        payload, templates = self.file_store.load()
        new_id = self.file_store.next_id(payload, templates)
        now = _now_text()
        dto = DietTemplateDTO(
            id=new_id,
            name=self._normalize_name(name),
            diet_text=str(diet_text or ""),
            schedule_json=normalize_schedule(schedule_json),
            is_default=1 if is_default else 0,
            version=1,
            created_at=now,
            updated_at=now,
            last_modified_by="doctor",
        )
        templates.append(dto)
        self.file_store.save_templates(templates, next_id=new_id + 1)
        return new_id

    def update_template(
        self,
        template_id: int,
        name: str,
        diet_text: str = "",
        schedule_json: Any = None,
        is_default: bool = False,
        expected_version: Optional[int] = None,
    ):
        payload, templates = self.file_store.load()
        current = self._find_template_in_list(templates, template_id)
        if not current:
            raise ValueError("Шаблон питания не найден")
        expected = int(expected_version if expected_version is not None else current.version or 0)
        if expected > 0 and int(current.version or 0) != expected:
            raise OptimisticLockError("Шаблон питания был изменен другим пользователем")
        dto = DietTemplateDTO(
            id=int(template_id),
            name=self._normalize_name(name),
            diet_text=str(diet_text or ""),
            schedule_json=normalize_schedule(schedule_json),
            is_default=1 if is_default else 0,
            version=int(current.version or 0) + 1,
            created_at=current.created_at,
            updated_at=_now_text(),
            last_modified_by="doctor",
        )
        updated = [dto if int(t.id) == int(template_id) else t for t in templates]
        self.file_store.save_templates(updated, next_id=self.file_store.next_id(payload, templates))

    def delete_template(self, template_id: int, expected_version: Optional[int] = None):
        payload, templates = self.file_store.load()
        current = self._find_template_in_list(templates, template_id)
        if not current:
            raise ValueError("Шаблон питания не найден")
        if expected_version is not None and int(expected_version) > 0 and int(current.version or 0) != int(expected_version):
            raise OptimisticLockError("Шаблон питания был изменен другим пользователем")
        remaining = [t for t in templates if int(t.id) != int(template_id)]
        self.file_store.save_templates(remaining, next_id=self.file_store.next_id(payload, templates))

    @staticmethod
    def _normalize_name(name: str) -> str:
        normalized = str(name or "").strip()
        if not normalized:
            raise ValueError("Название шаблона питания обязательно")
        return normalized

    def _ensure_file_initialized(self):
        self.file_store.initialize_from_seed()

    def _find_template(self, template_id: int) -> Optional[DietTemplateDTO]:
        templates = self.list_templates()
        return self._find_template_in_list(templates, template_id)

    @staticmethod
    def _find_template_in_list(templates: List[DietTemplateDTO], template_id: int) -> Optional[DietTemplateDTO]:
        for template in templates:
            if int(template.id or 0) == int(template_id):
                return template
        return None


class DietPlanService:
    def __init__(self, dao: DietPlanDAO, template_service: DietTemplateService):
        self.dao = dao
        self.template_service = template_service

    def shift_start_for_date(self, shift_date: datetime) -> datetime:
        start, _ = ShiftService.get_day_period(shift_date)
        return normalize_minute(start)

    def get_plan(self, admission_id: int, shift_date: datetime) -> Optional[DietPlanDTO]:
        return self.dao.get_plan(int(admission_id), self.shift_start_for_date(shift_date))

    def apply_template(
        self,
        admission_id: int,
        shift_date: datetime,
        template_id: int,
        expected_version: Optional[int] = None,
    ):
        template = self.template_service.get_template(template_id)
        stored_template_id = int(template.id or template_id)
        dto = DietPlanDTO(
            admission_id=int(admission_id),
            shift_start=self.shift_start_for_date(shift_date),
            template_id=stored_template_id,
            diet_text=template.diet_text,
            schedule_json=template.schedule_json,
            last_modified_by="doctor",
        )
        with self.dao.db.remcard_transaction(source="diet_plan_apply_template") as cur:
            self._sync_template_row_for_fk(cur, template)
            return self.dao.upsert_plan(dto, expected_version=expected_version, cursor=cur)

    def upsert_plan(
        self,
        admission_id: int,
        shift_date: datetime,
        diet_text: str,
        schedule_json: Any,
        template_id: Optional[int] = None,
        expected_version: Optional[int] = None,
    ):
        template = None
        stored_template_id = None
        if template_id is not None:
            template = self.template_service.get_template(int(template_id))
            stored_template_id = int(template.id or template_id)

        dto = DietPlanDTO(
            admission_id=int(admission_id),
            shift_start=self.shift_start_for_date(shift_date),
            template_id=stored_template_id,
            diet_text=str(diet_text or ""),
            schedule_json=normalize_schedule(schedule_json),
            last_modified_by="doctor",
        )
        if template is None:
            return self.dao.upsert_plan(dto, expected_version=expected_version)

        with self.dao.db.remcard_transaction(source="diet_plan_upsert") as cur:
            self._sync_template_row_for_fk(cur, template)
            return self.dao.upsert_plan(dto, expected_version=expected_version, cursor=cur)

    def delete_plan(self, admission_id: int, shift_date: datetime, expected_version: Optional[int] = None):
        self.dao.delete_plan(
            int(admission_id),
            self.shift_start_for_date(shift_date),
            expected_version=expected_version,
        )

    @staticmethod
    def _sync_template_row_for_fk(cursor, template: DietTemplateDTO):
        template_id = int(template.id or 0)
        if template_id <= 0:
            return

        version = int(template.version or 1)
        now = _now_text()
        cursor.execute("SELECT id FROM diet_templates WHERE id = ?", (template_id,))
        if cursor.fetchone():
            cursor.execute(
                """
                UPDATE diet_templates
                SET name = ?,
                    diet_text = ?,
                    schedule_json = ?,
                    is_default = ?,
                    version = ?,
                    last_modified_by = ?,
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                """,
                (
                    template.name,
                    template.diet_text,
                    template.schedule_json,
                    int(template.is_default or 0),
                    version,
                    template.last_modified_by or "doctor",
                    template_id,
                ),
            )
            return

        cursor.execute(
            """
            INSERT INTO diet_templates (
                id, name, diet_text, schedule_json, is_default, version,
                created_at, updated_at, last_modified_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, STRFTIME('%Y-%m-%d %H:%M:%f', 'now'), ?)
            """,
            (
                template_id,
                template.name,
                template.diet_text,
                template.schedule_json,
                int(template.is_default or 0),
                version,
                template.created_at or now,
                template.last_modified_by or "doctor",
            ),
        )


class OralIntakeService:
    def __init__(self, dao: OralIntakeDAO, vital_service, diet_plan_service: Optional[DietPlanService] = None):
        self.dao = dao
        self.vital_service = vital_service
        self.diet_plan_service = diet_plan_service

    def normalize_event_time(self, event_time: Optional[datetime] = None) -> datetime:
        dt = normalize_minute(event_time or datetime.now())
        now = normalize_minute(datetime.now())
        if dt > now:
            raise ValueError("Факт перорального ввода не может быть в будущем")
        return dt

    def shift_start_for_event(self, event_time: datetime) -> datetime:
        start, _ = ShiftService.get_day_period(event_time)
        return normalize_minute(start)

    def get_events(self, admission_id: int, shift_date: datetime) -> List[OralIntakeEventDTO]:
        start, end = self.vital_service.get_effective_bounds(int(admission_id), shift_date)
        return self.dao.get_events(int(admission_id), start, end)

    def apply_changes(self, admission_id: int, changes: list[dict]) -> Optional[OralIntakeEventDTO]:
        result = None
        with self.dao.db.remcard_transaction(source="oral_intake_batch") as cur:
            for change in changes or []:
                event_dt = normalize_minute(change["event_dt"])
                amount_ml = change.get("amount")
                expected_version = change.get("expected_version")
                result = self._apply_change_in_transaction(
                    cur,
                    admission_id=int(admission_id),
                    event_dt=event_dt,
                    amount_ml=amount_ml,
                    expected_version=expected_version,
                )
        return result

    def undo_changes(self, admission_id: int, shift_date: datetime, undo_batch: list[dict]):
        current_events = self.get_events(int(admission_id), shift_date)
        changes = []
        for change in reversed(undo_batch or []):
            event_dt = normalize_minute(change["event_dt"])
            current_event = self._event_for_time_in(current_events, event_dt)
            expected_version = getattr(current_event, "version", None)
            if change.get("before_amount") is None and current_event is None:
                continue
            changes.append(
                {
                    "event_dt": event_dt,
                    "amount": change.get("before_amount"),
                    "expected_version": expected_version,
                }
            )
        if not changes:
            return None
        return self.apply_changes(int(admission_id), changes)

    def upsert_event(
        self,
        admission_id: int,
        event_time: datetime,
        amount_ml: Optional[float],
        expected_version: Optional[int] = None,
    ):
        event_dt = self.normalize_event_time(event_time)
        is_ok, msg = self.vital_service.validate_timestamp(int(admission_id), event_dt, self.shift_start_for_event(event_dt))
        if not is_ok:
            raise ValueError(msg)

        if amount_ml is None or float(amount_ml) <= 0:
            return self.delete_event(
                admission_id=int(admission_id),
                event_time=event_dt,
                expected_version=expected_version,
            )

        dto = OralIntakeEventDTO(
            admission_id=int(admission_id),
            shift_start=self.shift_start_for_event(event_dt),
            event_time=event_dt,
            amount_ml=float(amount_ml),
            last_modified_by="nurse",
        )
        try:
            return self.dao.upsert_event(dto, expected_version=expected_version)
        except OptimisticLockError:
            current = self.dao.get_event_at(int(admission_id), event_dt)
            if current and abs(float(current.amount_ml) - float(amount_ml)) < 0.001:
                return current
            raise

    def add_event(self, admission_id: int, amount_ml: float, event_time: Optional[datetime] = None):
        return self.upsert_event(int(admission_id), event_time or datetime.now(), amount_ml)

    def delete_event(
        self,
        admission_id: int,
        event_time: datetime,
        expected_version: Optional[int] = None,
    ):
        event_dt = self.normalize_event_time(event_time)
        try:
            self.dao.delete_event(int(admission_id), event_dt, expected_version=expected_version)
        except OptimisticLockError:
            current = self.dao.get_event_at(int(admission_id), event_dt)
            if current is None:
                return None
            raise
        return None

    def _apply_change_in_transaction(
        self,
        cursor,
        *,
        admission_id: int,
        event_dt: datetime,
        amount_ml: Optional[float],
        expected_version: Optional[int],
    ) -> Optional[OralIntakeEventDTO]:
        event_dt = self.normalize_event_time(event_dt)
        is_ok, msg = self.vital_service.validate_timestamp(
            int(admission_id),
            event_dt,
            self.shift_start_for_event(event_dt),
        )
        if not is_ok:
            raise ValueError(msg)

        if amount_ml is None or float(amount_ml) <= 0:
            try:
                self.dao.delete_event(
                    int(admission_id),
                    event_dt,
                    expected_version=expected_version,
                    cursor=cursor,
                )
            except OptimisticLockError:
                current = self.dao.get_event_at(int(admission_id), event_dt, cursor=cursor)
                if current is None:
                    return None
                raise
            return None

        dto = OralIntakeEventDTO(
            admission_id=int(admission_id),
            shift_start=self.shift_start_for_event(event_dt),
            event_time=event_dt,
            amount_ml=float(amount_ml),
            last_modified_by="nurse",
        )
        try:
            return self.dao.upsert_event(dto, expected_version=expected_version, cursor=cursor)
        except OptimisticLockError:
            current = self.dao.get_event_at(int(admission_id), event_dt, cursor=cursor)
            if current and abs(float(current.amount_ml) - float(amount_ml)) < 0.001:
                return current
            raise

    @staticmethod
    def _event_for_time_in(events, event_dt: datetime):
        key = _dt_to_db(normalize_minute(event_dt))
        for event in events or []:
            if _dt_to_db(event.event_time) == key:
                return event
        return None

    def get_totals(self, admission_id: int, shift_date: datetime, current_time: Optional[datetime] = None) -> dict:
        start, end = self.vital_service.get_effective_bounds(int(admission_id), shift_date)
        calc_time = current_time or datetime.now()
        if calc_time < start:
            calc_time = start
        if calc_time >= end:
            calc_time = end

        events = self.dao.get_events(int(admission_id), start, end)
        plan = None
        if self.diet_plan_service is not None:
            plan = self.diet_plan_service.get_plan(int(admission_id), shift_date)
        return self._calculate_totals(events, plan, shift_date, start, end, normalize_minute(calc_time))

    @staticmethod
    def _calculate_totals(
        events: List[OralIntakeEventDTO],
        plan: Optional[DietPlanDTO],
        shift_date: datetime,
        start: datetime,
        end: datetime,
        current_time: datetime,
    ) -> dict:
        planned_by_time = {}
        if plan is not None:
            for item in schedule_items(plan.schedule_json):
                planned_dt = ShiftService.resolve_datetime(str(item["time"]), shift_date)
                if start <= planned_dt < end:
                    key = _dt_to_db(planned_dt)
                    planned_by_time[key] = planned_by_time.get(key, 0.0) + float(item["amount"])

        current = 0.0
        unplanned_daily = 0.0
        current_limit = normalize_minute(current_time)

        for event in events or []:
            event_dt = normalize_minute(event.event_time)
            amount = float(event.amount_ml or 0.0)
            if event_dt <= current_limit:
                current += amount
            # Плановые строки остаются планом; PRN/внеплановый факт добавляем к прогнозу отдельно.
            if _dt_to_db(event_dt) not in planned_by_time:
                unplanned_daily += amount

        daily = sum(planned_by_time.values()) + unplanned_daily
        return {"current": round(current, 1), "daily": round(daily, 1)}
