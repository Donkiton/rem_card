import json
import os
import tempfile
import uuid
from datetime import datetime, timedelta
from typing import Any, List, Optional

from rem_card.app.paths import SEED_DIR, USER_DICT_DIR
from rem_card.data.dao.diet_dao import DietPlanDAO, DietPlanVersionDAO, OralIntakeDAO
from rem_card.data.dao.exceptions import OptimisticLockError
from rem_card.data.dto.remcard_dto import DietPlanDTO, DietPlanVersionDTO, DietTemplateDTO, OralIntakeEventDTO
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
    seen_keys = set()
    for index, item in enumerate(raw_items, start=1):
        if not isinstance(item, dict):
            raise ValueError("Строка расписания питания должна быть объектом")
        time_text = str(item.get("time") or "").strip()
        if not ShiftService.is_time_input_valid(time_text):
            raise ValueError("Время питания должно быть в формате HH:mm")
        normalized_time = ShiftService.normalize_time(time_text)
        item_key = str(item.get("key") or f"{normalized_time}-{index}").strip()
        if item_key in seen_keys:
            raise ValueError("В расписании питания не должно быть повторяющихся строк")
        seen_keys.add(item_key)
        amount = int(float(item.get("amount") or 0))
        if amount <= 0:
            raise ValueError("Объем питания должен быть больше 0 мл")
        normalized.append(
            {
                "key": item_key,
                "meal": str(item.get("meal") or item.get("name") or "Приём пищи").strip(),
                "time": normalized_time,
                "amount": amount,
                "note": str(item.get("note") or "").strip(),
            }
        )

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
        normalized_time = ShiftService.normalize_time(time_text)
        result.append(
            {
                "key": str(item.get("key") or f"{normalized_time}-{len(result) + 1}"),
                "meal": str(item.get("meal") or item.get("name") or "Приём пищи"),
                "time": normalized_time,
                "amount": amount,
                "note": str(item.get("note") or ""),
            }
        )
    result.sort(key=lambda item: ((int(str(item["time"])[:2]) - 8) % 24, int(str(item["time"])[3:5])))
    return result


def normalize_diet_details(details: Any) -> str:
    if details is None or details == "":
        raw = {}
    elif isinstance(details, str):
        raw = json.loads(details)
    else:
        raw = dict(details)
    if not isinstance(raw, dict):
        raise ValueError("Дополнительные параметры диеты должны быть объектом")
    daily_fluid = raw.get("daily_fluid_ml")
    if daily_fluid in (None, ""):
        daily_fluid = None
    else:
        daily_fluid = max(0, int(float(daily_fluid)))
    normalized = {
        "consistency": str(raw.get("consistency") or "").strip(),
        "temperature": str(raw.get("temperature") or "").strip(),
        "salt_limit": str(raw.get("salt_limit") or "").strip(),
        "fractional": bool(raw.get("fractional", False)),
        "daily_fluid_ml": daily_fluid,
        "special_instructions": str(raw.get("special_instructions") or "").strip(),
        "comment": str(raw.get("comment") or "").strip(),
        "no_food": bool(raw.get("no_food", False)),
        "no_fluids": bool(raw.get("no_fluids", False)),
        "on_demand": bool(raw.get("on_demand", False)),
    }
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def diet_details(details_json: str) -> dict[str, Any]:
    try:
        return json.loads(normalize_diet_details(details_json))
    except Exception:
        return json.loads(normalize_diet_details({}))


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
        ordered = list(templates or [])
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
                    details_json=normalize_diet_details(raw.get("details", raw.get("details_json", {}))),
                    is_default=_as_bool_int(raw.get("is_default", raw.get("default", False))),
                    version=self._coerce_int(raw.get("version"), default=1),
                    created_at=str(raw.get("created_at") or now),
                    updated_at=str(raw.get("updated_at") or now),
                    last_modified_by=str(raw.get("last_modified_by") or "doctor"),
                )
            )
        return templates

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
    def _dto_to_json(template: DietTemplateDTO) -> dict[str, Any]:
        return {
            "id": int(template.id or 0),
            "name": template.name or "",
            "diet_text": template.diet_text or "",
            "schedule": schedule_items(template.schedule_json),
            "details": diet_details(template.details_json),
            "is_default": bool(template.is_default),
            "version": int(template.version or 1),
            "created_at": template.created_at or _now_text(),
            "updated_at": template.updated_at or _now_text(),
            "last_modified_by": template.last_modified_by or "doctor",
        }


class DietTemplateService:
    def __init__(self, file_store: Optional[DietTemplateFileStore] = None, settings_service: Any = None):
        self.file_store = file_store
        if self.file_store is not None:
            self._ensure_file_initialized()
            self.settings_service = None
        else:
            from rem_card.services.settings.settings_service import get_settings_service

            self.settings_service = settings_service or get_settings_service()

    def list_templates(self) -> List[DietTemplateDTO]:
        if self.settings_service is not None:
            return self.settings_service.list_diet_templates()
        return self.file_store.list_templates()

    def get_template(self, template_id: int) -> DietTemplateDTO:
        if self.settings_service is not None:
            return self.settings_service.get_diet_template(template_id)
        template = self._find_template(template_id)
        if not template:
            raise ValueError("Шаблон питания не найден")
        return template

    def create_template(self, name: str, diet_text: str = "", schedule_json: Any = None, is_default: bool = False, details_json: Any = None):
        if self.settings_service is not None:
            return self.settings_service.create_diet_template(name, diet_text, schedule_json, is_default, details_json)
        payload, templates = self.file_store.load()
        new_id = self.file_store.next_id(payload, templates)
        now = _now_text()
        dto = DietTemplateDTO(
            id=new_id,
            name=self._normalize_name(name),
            diet_text=str(diet_text or ""),
            schedule_json=normalize_schedule(schedule_json),
            details_json=normalize_diet_details(details_json),
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
        details_json: Any = None,
        expected_version: Optional[int] = None,
    ):
        if self.settings_service is not None:
            return self.settings_service.update_diet_template(
                template_id,
                name,
                diet_text,
                schedule_json,
                is_default,
                details_json,
                expected_version=expected_version,
            )
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
            details_json=normalize_diet_details(details_json),
            is_default=1 if is_default else 0,
            version=int(current.version or 0) + 1,
            created_at=current.created_at,
            updated_at=_now_text(),
            last_modified_by="doctor",
        )
        updated = [dto if int(t.id) == int(template_id) else t for t in templates]
        self.file_store.save_templates(updated, next_id=self.file_store.next_id(payload, templates))

    def delete_template(self, template_id: int, expected_version: Optional[int] = None):
        if self.settings_service is not None:
            return self.settings_service.delete_diet_template(template_id, expected_version=expected_version)
        payload, templates = self.file_store.load()
        current = self._find_template_in_list(templates, template_id)
        if not current:
            raise ValueError("Шаблон питания не найден")
        if expected_version is not None and int(expected_version) > 0 and int(current.version or 0) != int(expected_version):
            raise OptimisticLockError("Шаблон питания был изменен другим пользователем")
        remaining = [t for t in templates if int(t.id) != int(template_id)]
        self.file_store.save_templates(remaining, next_id=self.file_store.next_id(payload, templates))

    def reorder_templates(self, ordered_template_ids: list[int]):
        if self.settings_service is not None:
            return self.settings_service.reorder_diet_templates(ordered_template_ids)
        payload, templates = self.file_store.load()
        templates_by_id = {int(t.id): t for t in templates if t.id is not None}
        ordered_ids: list[int] = []
        for raw_id in ordered_template_ids or []:
            try:
                template_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if template_id in templates_by_id and template_id not in ordered_ids:
                ordered_ids.append(template_id)

        missing_ids = [
            int(t.id)
            for t in templates
            if t.id is not None and int(t.id) not in ordered_ids
        ]
        if not ordered_ids and templates:
            raise ValueError("Не указан порядок шаблонов питания")

        reordered = [templates_by_id[template_id] for template_id in ordered_ids + missing_ids]
        if [int(t.id) for t in reordered if t.id is not None] == [int(t.id) for t in templates if t.id is not None]:
            return
        self.file_store.save_templates(reordered, next_id=self.file_store.next_id(payload, templates))

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
    def __init__(
        self,
        dao: DietPlanDAO,
        template_service: DietTemplateService,
        version_dao: Optional[DietPlanVersionDAO] = None,
    ):
        self.dao = dao
        self.template_service = template_service
        self.version_dao = version_dao

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
            details_json=template.details_json,
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
        details_json: Any = None,
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
            details_json=normalize_diet_details(details_json),
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

    def list_versions(self, admission_id: int, shift_date: datetime) -> List[DietPlanVersionDTO]:
        if self.version_dao is None:
            return []
        start, end = ShiftService.get_day_period(shift_date)
        return self.version_dao.list_versions(int(admission_id), normalize_minute(start), normalize_minute(end))

    def list_all_versions(self, admission_id: int) -> List[DietPlanVersionDTO]:
        return self.version_dao.list_all(int(admission_id)) if self.version_dao is not None else []

    def active_at(self, admission_id: int, moment: datetime) -> Optional[DietPlanVersionDTO]:
        return self.version_dao.get_active_at(int(admission_id), normalize_minute(moment)) if self.version_dao else None

    def assign_version(
        self,
        admission_id: int,
        effective_from: datetime,
        *,
        template_id: Optional[int] = None,
        diet_name: str = "",
        diet_text: str = "",
        schedule_json: Any = None,
        details_json: Any = None,
        change_note: str = "",
        version_id: Optional[int] = None,
        expected_version: Optional[int] = None,
    ) -> DietPlanVersionDTO:
        if self.version_dao is None:
            raise RuntimeError("История назначений диеты недоступна")
        effective = normalize_minute(effective_from)
        template = self.template_service.get_template(int(template_id)) if template_id is not None else None
        if template is not None:
            stored_template_id = int(template.id or template_id)
            resolved_name = template.name
            resolved_text = template.diet_text
            resolved_schedule = template.schedule_json
            resolved_details = template.details_json
        else:
            stored_template_id = None
            resolved_name = str(diet_name or diet_text or "Индивидуальная диета").strip()
            resolved_text = str(diet_text or "").strip()
            resolved_schedule = normalize_schedule(schedule_json)
            resolved_details = normalize_diet_details(details_json)
        shift_start = self.shift_start_for_date(effective)
        dto = DietPlanVersionDTO(
            id=version_id,
            admission_id=int(admission_id), shift_start=shift_start, effective_from=effective,
            template_id=stored_template_id, diet_name=resolved_name, diet_text=resolved_text,
            schedule_json=resolved_schedule, details_json=resolved_details,
            change_note=str(change_note or "").strip(), last_modified_by="doctor",
        )
        previous_shift = None
        if version_id is not None:
            previous = self.version_dao.get_by_id(int(version_id))
            previous_shift = previous.shift_start if previous is not None else None
        with self.dao.db.remcard_transaction(source="diet_plan_version_assign") as cur:
            if template is not None:
                self._sync_template_row_for_fk(cur, template)
            if version_id is not None:
                version = self.version_dao.update_version_by_id(
                    dto, expected_version=expected_version, cursor=cur
                )
            else:
                version = self.version_dao.upsert_version(
                    dto, expected_version=expected_version, cursor=cur
                )
            # Compatibility anchors keep reports and older clients on the latest
            # effective assignment for each affected medical day.
            affected_shifts = {shift_start}
            if previous_shift is not None:
                affected_shifts.add(previous_shift)
            for affected_shift in affected_shifts:
                self._sync_day_anchor(cur, int(admission_id), affected_shift)
            return version

    def delete_version(
        self,
        admission_id: int,
        version_id: int,
        *,
        expected_version: Optional[int] = None,
    ) -> None:
        if self.version_dao is None:
            raise RuntimeError("История назначений диеты недоступна")
        current = self.version_dao.get_by_id(int(version_id))
        if current is None or int(current.admission_id) != int(admission_id):
            raise ValueError("Назначение диеты не найдено")

        with self.dao.db.remcard_transaction(source="diet_plan_version_delete") as cur:
            self.version_dao.delete_by_id(
                int(version_id),
                expected_version=expected_version,
                cursor=cur,
            )
            self._sync_day_anchor(cur, int(admission_id), current.shift_start)

    def _sync_day_anchor(self, cursor, admission_id: int, shift_start: datetime) -> None:
        cursor.execute(
            """
            SELECT * FROM diet_plan_versions
            WHERE admission_id = ? AND shift_start = ?
            ORDER BY DATETIME(effective_from) DESC, id DESC LIMIT 1
            """,
            (int(admission_id), _dt_to_db(shift_start)),
        )
        row = cursor.fetchone()
        if row is None:
            self.dao.delete_plan(int(admission_id), shift_start, cursor=cursor)
            return
        latest = self.version_dao._map(row)
        self.dao.upsert_plan(
            DietPlanDTO(
                admission_id=int(admission_id), shift_start=shift_start,
                template_id=latest.template_id, diet_text=latest.diet_text,
                schedule_json=latest.schedule_json, details_json=latest.details_json,
                last_modified_by="doctor",
            ),
            cursor=cursor,
        )

    def planned_items_for_day(self, admission_id: int, shift_date: datetime) -> list[dict[str, Any]]:
        start, end = ShiftService.get_day_period(shift_date)
        start = normalize_minute(start)
        end = normalize_minute(end)
        versions = self.version_dao.list_versions(int(admission_id), start, end) if self.version_dao else []
        if not versions:
            plan = self.get_plan(int(admission_id), shift_date)
            versions = [
                DietPlanVersionDTO(
                    id=None, admission_id=int(admission_id), shift_start=start,
                    effective_from=start, diet_name=(plan.diet_text if plan else ""),
                    diet_text=(plan.diet_text if plan else ""),
                    schedule_json=(plan.schedule_json if plan else "[]"),
                    details_json=(plan.details_json if plan else "{}"),
                )
            ] if plan else []
        result: list[dict[str, Any]] = []
        ordered = sorted(versions, key=lambda item: (item.effective_from, int(item.id or 0)))
        for index, version in enumerate(ordered):
            active_from = max(start, version.effective_from)
            active_to = end if index + 1 >= len(ordered) else min(end, ordered[index + 1].effective_from)
            for item in schedule_items(version.schedule_json):
                planned_dt = ShiftService.resolve_datetime(str(item["time"]), shift_date)
                if active_from <= planned_dt < active_to:
                    result.append(
                        {
                            **item,
                            "planned_dt": planned_dt,
                            "plan_version_id": version.id,
                            "diet_name": version.diet_name,
                        }
                    )
        result.sort(key=lambda item: item["planned_dt"])
        return result

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
                    details_json = ?,
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
                    template.details_json,
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
                id, name, diet_text, schedule_json, details_json, is_default, version,
                created_at, updated_at, last_modified_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, STRFTIME('%Y-%m-%d %H:%M:%f', 'now'), ?)
            """,
            (
                template_id,
                template.name,
                template.diet_text,
                template.schedule_json,
                template.details_json,
                int(template.is_default or 0),
                version,
                template.created_at or now,
                template.last_modified_by or "doctor",
            ),
        )


class OralIntakeService:
    PLANNED_ENTRY_EARLY_WINDOW = timedelta(hours=3)

    def __init__(self, dao: OralIntakeDAO, vital_service, diet_plan_service: Optional[DietPlanService] = None):
        self.dao = dao
        self.vital_service = vital_service
        self.diet_plan_service = diet_plan_service

    def normalize_event_time(self, event_time: Optional[datetime] = None) -> datetime:
        dt = normalize_minute(event_time or datetime.now())
        now = normalize_minute(datetime.now())
        if dt > now:
            raise ValueError("Время фактического питания не может быть в будущем")
        return dt

    def normalize_planned_fact_time(self, event_time: datetime, planned_time: datetime) -> datetime:
        event_dt = normalize_minute(event_time)
        planned_dt = normalize_minute(planned_time)
        shift_start, shift_end = ShiftService.get_day_period(planned_dt)
        earliest = max(
            normalize_minute(shift_start),
            planned_dt - self.PLANNED_ENTRY_EARLY_WINDOW,
        )
        if event_dt < earliest:
            raise ValueError(
                "Не следует кормить пациента раньше назначенного времени. "
                "Данные можно внести не ранее чем за 3 часа до запланированного приёма пищи."
            )
        if event_dt >= normalize_minute(shift_end):
            raise ValueError(
                "Время фактического питания должно находиться в пределах текущих "
                "медицинских суток (08:00–08:00)."
            )
        return event_dt

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

    def create_fact(
        self,
        admission_id: int,
        event_time: datetime,
        amount_ml: float,
        *,
        plan_version_id: Optional[int] = None,
        planned_item_key: Optional[str] = None,
        planned_time: Optional[datetime] = None,
        entry_kind: str = "unplanned",
        meal_name: str = "",
        note: str = "",
        actor: str = "nurse",
        action_id: Optional[str] = None,
    ) -> OralIntakeEventDTO:
        event_dt = (
            self.normalize_planned_fact_time(event_time, planned_time)
            if planned_time is not None
            else self.normalize_event_time(event_time)
        )
        is_ok, msg = self.vital_service.validate_timestamp(
            int(admission_id), event_dt, self.shift_start_for_event(event_dt)
        )
        if not is_ok:
            raise ValueError(msg)
        if float(amount_ml or 0) <= 0:
            raise ValueError("Фактический объём должен быть больше 0 мл")
        normalized_entry_kind = str(entry_kind or "unplanned")
        dto = OralIntakeEventDTO(
            admission_id=int(admission_id), shift_start=self.shift_start_for_event(event_dt),
            event_time=event_dt, amount_ml=float(amount_ml),
            plan_version_id=plan_version_id, planned_item_key=planned_item_key,
            entry_kind=normalized_entry_kind, meal_name=str(meal_name or ""),
            note=str(note or ""), action_id=action_id or uuid.uuid4().hex,
            last_modified_by=str(actor or "nurse"),
        )
        if normalized_entry_kind == "unplanned":
            return self.dao.upsert_unplanned_event(dto)
        return self.dao.create_event(dto)

    def update_fact(
        self,
        event_id: int,
        event_time: datetime,
        amount_ml: float,
        *,
        note: str = "",
        meal_name: Optional[str] = None,
        planned_time: Optional[datetime] = None,
        actor: str = "doctor",
        expected_version: Optional[int] = None,
    ) -> OralIntakeEventDTO:
        current = self.dao.get_event(int(event_id))
        if current is None:
            raise ValueError("Факт питания не найден")
        event_dt = (
            self.normalize_planned_fact_time(event_time, planned_time)
            if planned_time is not None
            else self.normalize_event_time(event_time)
        )
        is_ok, msg = self.vital_service.validate_timestamp(
            int(current.admission_id), event_dt, self.shift_start_for_event(event_dt)
        )
        if not is_ok:
            raise ValueError(msg)
        if float(amount_ml or 0) <= 0:
            raise ValueError("Фактический объём должен быть больше 0 мл")
        current.event_time = event_dt
        current.shift_start = self.shift_start_for_event(event_dt)
        current.amount_ml = float(amount_ml)
        current.note = str(note or "")
        if meal_name is not None:
            current.meal_name = str(meal_name or "")
        current.action_id = uuid.uuid4().hex
        current.last_modified_by = str(actor or "doctor")
        return self.dao.update_event_by_id(current, expected_version=expected_version)

    def delete_fact(self, event_id: int, *, expected_version: Optional[int] = None) -> None:
        self.dao.delete_event_by_id(int(event_id), expected_version=expected_version)

    def undo_last_action(self, admission_id: int, actor: str) -> int:
        action_id = self.dao.last_action_id(int(admission_id), str(actor or "nurse"))
        if not action_id:
            return 0
        return self.dao.delete_action(int(admission_id), action_id, actor=str(actor or "nurse"))

    def clear_facts(self, admission_id: int, *, before: Optional[datetime] = None) -> int:
        return self.dao.clear_events(int(admission_id), before=normalize_minute(before) if before else None)

    def active_restrictions(self, admission_id: int, moment: datetime) -> dict[str, Any]:
        if self.diet_plan_service is None:
            return {}
        version = self.diet_plan_service.active_at(int(admission_id), normalize_minute(moment))
        if version is None:
            return {}
        details = diet_details(version.details_json)
        return {
            "diet_name": version.diet_name,
            "no_food": bool(details.get("no_food")),
            "no_fluids": bool(details.get("no_fluids")),
            "version_id": version.id,
        }

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
        planned_items = None
        plan = None
        if self.diet_plan_service is not None:
            planned_items = self.diet_plan_service.planned_items_for_day(int(admission_id), shift_date)
            plan = self.diet_plan_service.get_plan(int(admission_id), shift_date)
        return self._calculate_totals(
            events, plan, shift_date, start, end, normalize_minute(calc_time), planned_items=planned_items
        )

    @staticmethod
    def _calculate_totals(
        events: List[OralIntakeEventDTO],
        plan: Optional[DietPlanDTO],
        shift_date: datetime,
        start: datetime,
        end: datetime,
        current_time: datetime,
        planned_items: Optional[list[dict[str, Any]]] = None,
    ) -> dict:
        planned_by_time = {}
        if planned_items is not None:
            for item in planned_items:
                planned_dt = item.get("planned_dt")
                if isinstance(planned_dt, datetime) and start <= planned_dt < end:
                    key = _dt_to_db(planned_dt)
                    planned_by_time[key] = planned_by_time.get(key, 0.0) + float(item.get("amount") or 0)
        elif plan is not None:
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
            if not event.planned_item_key and _dt_to_db(event_dt) not in planned_by_time:
                unplanned_daily += amount

        daily = sum(planned_by_time.values()) + unplanned_daily
        return {"current": round(current, 1), "daily": round(daily, 1)}
