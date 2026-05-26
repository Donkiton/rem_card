from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from rem_card.app.paths import USER_DICT_DIR
from rem_card.data.dto.lab_orders_dto import LAB_MATERIAL_LABELS, LabMaterial
from rem_card.services.shift_service import ShiftService


LAB_ANALYSIS_TEMPLATES_FILE_NAME = "lab_analysis_templates.json"

DEFAULT_LAB_ANALYSIS_TEMPLATES: tuple[dict[str, Any], ...] = (
    {"code": "cbc", "name": "Общий анализ крови", "material": LabMaterial.VENOUS_BLOOD.value},
    {"code": "blood_biochemistry", "name": "Биохимический анализ крови", "material": LabMaterial.VENOUS_BLOOD.value},
    {"code": "electrolytes", "name": "Электролиты крови", "material": LabMaterial.VENOUS_BLOOD.value},
    {"code": "coagulogram", "name": "Коагулограмма", "material": LabMaterial.VENOUS_BLOOD.value},
    {"code": "crp", "name": "С-реактивный белок", "material": LabMaterial.VENOUS_BLOOD.value},
    {"code": "pct", "name": "Прокальцитонин", "material": LabMaterial.VENOUS_BLOOD.value},
    {"code": "blood_glucose", "name": "Глюкоза крови", "material": LabMaterial.VENOUS_BLOOD.value},
    {"code": "arterial_blood_gas", "name": "КЩС и газы артериальной крови", "material": LabMaterial.ARTERIAL_BLOOD.value},
    {"code": "urinalysis", "name": "Общий анализ мочи", "material": LabMaterial.URINE.value},
    {"code": "csf_general", "name": "Общий анализ ликвора", "material": LabMaterial.LIQUOR.value},
)

DEFAULT_LAB_MATERIALS: tuple[dict[str, Any], ...] = tuple(
    {"code": code, "label": label, "built_in": True}
    for code, label in LAB_MATERIAL_LABELS.items()
)


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _material_label_map(materials: list["LabMaterialDTO"] | None = None) -> dict[str, str]:
    labels = dict(LAB_MATERIAL_LABELS)
    for material in materials or []:
        code = str(material.code or "").strip()
        label = str(material.label or "").strip()
        if code and label:
            labels[code] = label
    return labels


def normalize_lab_material(value: Any, material_labels: dict[str, str] | None = None) -> str:
    text = str(value or "").strip()
    raw = text.lower()
    if not raw:
        return LabMaterial.VENOUS_BLOOD.value

    labels = dict(LAB_MATERIAL_LABELS)
    labels.update(material_labels or {})
    aliases = {
        LabMaterial.VENOUS_BLOOD.value: LabMaterial.VENOUS_BLOOD.value,
        "venous": LabMaterial.VENOUS_BLOOD.value,
        "кровь венозная": LabMaterial.VENOUS_BLOOD.value,
        "венозная кровь": LabMaterial.VENOUS_BLOOD.value,
        LabMaterial.ARTERIAL_BLOOD.value: LabMaterial.ARTERIAL_BLOOD.value,
        "arterial": LabMaterial.ARTERIAL_BLOOD.value,
        "кровь артериальная": LabMaterial.ARTERIAL_BLOOD.value,
        "артериальная кровь": LabMaterial.ARTERIAL_BLOOD.value,
        LabMaterial.URINE.value: LabMaterial.URINE.value,
        "моча": LabMaterial.URINE.value,
        LabMaterial.LIQUOR.value: LabMaterial.LIQUOR.value,
        "csf": LabMaterial.LIQUOR.value,
        "ликвор": LabMaterial.LIQUOR.value,
    }
    for code, label in labels.items():
        aliases[str(code or "").strip().lower()] = str(code)
        aliases[str(label or "").strip().lower()] = str(code)
    return aliases.get(raw, text)


def normalize_lab_times(values: Any) -> list[str]:
    if values is None or values == "":
        raw_values = []
    elif isinstance(values, str):
        raw_values = [item.strip() for item in values.split(",")]
    else:
        raw_values = list(values or [])

    normalized: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        time_text = str(raw or "").strip()
        if not time_text:
            continue
        if not ShiftService.is_time_input_valid(time_text):
            raise ValueError("Время анализа должно быть в формате HH:mm")
        normalized_time = ShiftService.normalize_time(time_text)
        if normalized_time in seen:
            continue
        seen.add(normalized_time)
        normalized.append(normalized_time)
    normalized.sort(key=lambda item: ((int(item[:2]) - 8) % 24, int(item[3:5])))
    return normalized


@dataclass
class LabMaterialDTO:
    code: str
    label: str
    built_in: bool = False
    version: int = 1
    created_at: str = ""
    updated_at: str = ""
    last_modified_by: str = "doctor"

    def as_dict(self) -> dict[str, Any]:
        now = _now_text()
        return {
            "code": self.code,
            "label": self.label,
            "built_in": bool(self.built_in),
            "version": int(self.version or 1),
            "created_at": self.created_at or now,
            "updated_at": self.updated_at or now,
            "last_modified_by": self.last_modified_by or "doctor",
        }


@dataclass
class LabAnalysisTemplateDTO:
    id: int
    code: str
    name: str
    material: str = LabMaterial.VENOUS_BLOOD.value
    comment: str = ""
    default_times: list[str] = field(default_factory=list)
    version: int = 1
    created_at: str = ""
    updated_at: str = ""
    last_modified_by: str = "doctor"

    def as_dict(self, material_labels: dict[str, str] | None = None) -> dict[str, Any]:
        labels = dict(LAB_MATERIAL_LABELS)
        labels.update(material_labels or {})
        material = normalize_lab_material(self.material, labels)
        return {
            "id": int(self.id),
            "code": self.code,
            "name": self.name,
            "material": material,
            "material_label": labels.get(material, material),
            "comment": self.comment or "",
            "default_times": list(self.default_times or []),
            "version": int(self.version or 1),
            "created_at": self.created_at or _now_text(),
            "updated_at": self.updated_at or _now_text(),
            "last_modified_by": self.last_modified_by or "doctor",
        }


class LabAnalysisCatalogFileStore:
    def __init__(self, path: Optional[str] = None):
        self.path = path or os.path.join(USER_DICT_DIR, LAB_ANALYSIS_TEMPLATES_FILE_NAME)

    def exists(self) -> bool:
        return os.path.exists(self.path)

    def load(self) -> tuple[dict[str, Any], list[LabAnalysisTemplateDTO]]:
        payload, templates, _materials = self.load_catalog()
        return payload, templates

    def load_catalog(self) -> tuple[dict[str, Any], list[LabAnalysisTemplateDTO], list[LabMaterialDTO]]:
        payload = self._read_payload()
        materials = self._materials_from_payload(payload)
        return payload, self._templates_from_payload(payload, materials), materials

    def list_templates(self) -> list[LabAnalysisTemplateDTO]:
        _, templates = self.load()
        return templates

    def list_materials(self) -> list[LabMaterialDTO]:
        _payload, _templates, materials = self.load_catalog()
        return materials

    def save_templates(
        self,
        templates: list[LabAnalysisTemplateDTO],
        *,
        next_id: Optional[int] = None,
        materials: Optional[list[LabMaterialDTO]] = None,
    ):
        if materials is None:
            materials = self._materials_from_payload(self._read_payload())
        self.save_catalog(templates, materials, next_id=next_id)

    def save_catalog(
        self,
        templates: list[LabAnalysisTemplateDTO],
        materials: list[LabMaterialDTO],
        *,
        next_id: Optional[int] = None,
    ):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        ordered = list(templates or [])
        ordered_materials = list(materials or [])
        material_labels = _material_label_map(ordered_materials)
        max_id = max((int(item.id or 0) for item in ordered), default=0)
        payload = {
            "next_id": int(next_id if next_id is not None else max_id + 1),
            "materials": [item.as_dict() for item in ordered_materials],
            "templates": [item.as_dict(material_labels) for item in ordered],
        }
        directory = os.path.dirname(self.path)
        fd, tmp_path = tempfile.mkstemp(prefix=".lab_analysis_templates_", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(tmp_path, self.path)
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

    def initialize_from_defaults(self):
        if self.exists():
            self.load_catalog()
            return

        now = _now_text()
        materials = self._default_materials(now)
        material_labels = _material_label_map(materials)
        templates = [
            LabAnalysisTemplateDTO(
                id=index,
                code=str(raw.get("code") or f"analysis_{index}"),
                name=str(raw.get("name") or "").strip(),
                material=normalize_lab_material(raw.get("material"), material_labels),
                comment=str(raw.get("comment") or ""),
                default_times=normalize_lab_times(raw.get("default_times")),
                version=1,
                created_at=now,
                updated_at=now,
            )
            for index, raw in enumerate(DEFAULT_LAB_ANALYSIS_TEMPLATES, start=1)
        ]
        self.save_catalog(templates, materials, next_id=len(templates) + 1)

    def next_id(self, payload: dict[str, Any], templates: list[LabAnalysisTemplateDTO]) -> int:
        max_id = max((int(item.id or 0) for item in templates), default=0)
        try:
            configured_next = int(payload.get("next_id") or 0)
        except Exception:
            configured_next = 0
        return max(1, max_id + 1, configured_next)

    def _read_payload(self) -> dict[str, Any]:
        if not os.path.exists(self.path):
            return {"next_id": 1, "templates": []}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Файл справочника анализов поврежден: {self.path} ({exc})") from exc

        if isinstance(payload, list):
            return {"templates": payload}
        if not isinstance(payload, dict):
            raise ValueError(f"Файл справочника анализов должен быть JSON-объектом: {self.path}")
        return payload

    def _materials_from_payload(self, payload: dict[str, Any]) -> list[LabMaterialDTO]:
        now = _now_text()
        materials = self._default_materials(now)
        used_codes = {material.code for material in materials}
        used_labels = {material.label.strip().lower() for material in materials}
        raw_materials = payload.get("materials")
        if raw_materials is None:
            return materials
        if not isinstance(raw_materials, list):
            raise ValueError("Поле materials в lab_analysis_templates.json должно быть списком")

        for raw in raw_materials:
            if not isinstance(raw, dict) or raw.get("_deleted"):
                continue
            label = str(raw.get("label") or raw.get("name") or "").strip()
            if not label:
                continue
            raw_code = str(raw.get("code") or "").strip()
            if raw_code in LAB_MATERIAL_LABELS:
                continue
            code = self._coerce_material_code(raw_code or label, used_codes)
            normalized_label = label.lower()
            if normalized_label in used_labels:
                continue
            used_codes.add(code)
            used_labels.add(normalized_label)
            materials.append(
                LabMaterialDTO(
                    code=code,
                    label=label,
                    built_in=False,
                    version=self._coerce_int(raw.get("version"), default=1),
                    created_at=str(raw.get("created_at") or now),
                    updated_at=str(raw.get("updated_at") or now),
                    last_modified_by=str(raw.get("last_modified_by") or "doctor"),
                )
            )
        return materials

    def _templates_from_payload(
        self,
        payload: dict[str, Any],
        materials: list[LabMaterialDTO] | None = None,
    ) -> list[LabAnalysisTemplateDTO]:
        raw_templates = payload.get("templates", [])
        if not isinstance(raw_templates, list):
            raise ValueError("Поле templates в lab_analysis_templates.json должно быть списком")

        templates: list[LabAnalysisTemplateDTO] = []
        material_labels = _material_label_map(materials)
        used_ids: set[int] = set()
        used_codes: set[str] = set()
        now = _now_text()
        for index, raw in enumerate(raw_templates, start=1):
            if not isinstance(raw, dict) or raw.get("_deleted"):
                continue
            template_id = self._coerce_id(raw.get("id", index), used_ids)
            used_ids.add(template_id)
            name = str(raw.get("name") or "").strip()
            if not name:
                raise ValueError(f"В шаблоне анализа id={template_id} не указано название")
            code = self._coerce_code(raw.get("code"), name, template_id, used_codes)
            used_codes.add(code)
            templates.append(
                LabAnalysisTemplateDTO(
                    id=template_id,
                    code=code,
                    name=name,
                    material=normalize_lab_material(raw.get("material"), material_labels),
                    comment=str(raw.get("comment") or ""),
                    default_times=normalize_lab_times(raw.get("default_times")),
                    version=self._coerce_int(raw.get("version"), default=1),
                    created_at=str(raw.get("created_at") or now),
                    updated_at=str(raw.get("updated_at") or now),
                    last_modified_by=str(raw.get("last_modified_by") or "doctor"),
                )
            )
        return templates

    @staticmethod
    def _default_materials(now: str) -> list[LabMaterialDTO]:
        return [
            LabMaterialDTO(
                code=str(raw["code"]),
                label=str(raw["label"]),
                built_in=True,
                version=1,
                created_at=now,
                updated_at=now,
            )
            for raw in DEFAULT_LAB_MATERIALS
        ]

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

    @classmethod
    def _coerce_code(cls, value: Any, name: str, template_id: int, used_codes: set[str]) -> str:
        code = cls._slug(value) or cls._slug(name) or f"analysis_{int(template_id)}"
        base = code
        suffix = 2
        while code in used_codes:
            code = f"{base}_{suffix}"
            suffix += 1
        return code

    @classmethod
    def _coerce_material_code(cls, value: Any, used_codes: set[str]) -> str:
        code = cls._slug(value) or f"material_{len(used_codes) + 1}"
        base = code
        suffix = 2
        while code in used_codes:
            code = f"{base}_{suffix}"
            suffix += 1
        return code

    @staticmethod
    def _slug(value: Any) -> str:
        text = str(value or "").strip().lower()
        text = re.sub(r"[^a-z0-9_]+", "_", text)
        return re.sub(r"_+", "_", text).strip("_")


class LabAnalysisCatalogService:
    def __init__(self, file_store: Optional[LabAnalysisCatalogFileStore] = None, settings_service: Any = None):
        self.file_store = file_store
        if self.file_store is not None:
            self.file_store.initialize_from_defaults()
            self.settings_service = None
        else:
            from rem_card.services.settings.settings_service import get_settings_service

            self.settings_service = settings_service or get_settings_service()

    def list_templates(self) -> list[dict[str, Any]]:
        if self.settings_service is not None:
            return self.settings_service.list_lab_templates()
        _payload, templates, materials = self.file_store.load_catalog()
        material_labels = _material_label_map(materials)
        return [template.as_dict(material_labels) for template in templates]

    def list_materials(self) -> list[dict[str, Any]]:
        if self.settings_service is not None:
            return self.settings_service.list_lab_materials()
        return [material.as_dict() for material in self.file_store.list_materials()]

    def material_labels(self) -> dict[str, str]:
        if self.settings_service is not None:
            return self.settings_service.material_labels()
        return _material_label_map(self.file_store.list_materials())

    def create_material(self, *, name: str) -> str:
        if self.settings_service is not None:
            return self.settings_service.create_lab_material(name=name)
        label = str(name or "").strip()
        if not label:
            raise ValueError("Укажите название материала")
        payload, templates, materials = self.file_store.load_catalog()
        used_codes = {material.code for material in materials}
        used_labels = {material.label.strip().lower() for material in materials}
        if label.lower() in used_labels:
            raise ValueError("Такой материал уже есть в справочнике")
        code = LabAnalysisCatalogFileStore._coerce_material_code(label, used_codes)
        now = _now_text()
        materials.append(
            LabMaterialDTO(
                code=code,
                label=label,
                built_in=False,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        self.file_store.save_catalog(templates, materials, next_id=self.file_store.next_id(payload, templates))
        return code

    def save_materials(self, materials: list[dict[str, Any]]) -> None:
        if self.settings_service is not None:
            self.settings_service.save_lab_materials(materials)
            return
        payload, templates, current_materials = self.file_store.load_catalog()
        current_by_code = {material.code: material for material in current_materials}
        normalized: list[LabMaterialDTO] = []
        used_codes: set[str] = set()
        used_labels: set[str] = set()
        for index, raw in enumerate(materials or [], start=1):
            if not isinstance(raw, dict):
                continue
            label = " ".join(str(raw.get("label") or raw.get("name") or "").split())
            if not label:
                raise ValueError("Укажите название материала")
            label_key = label.lower()
            if label_key in used_labels:
                raise ValueError(f"Материал уже есть в списке: {label}")
            code = str(raw.get("code") or "").strip() or LabAnalysisCatalogFileStore._coerce_material_code(label, used_codes)
            if code in used_codes:
                code = LabAnalysisCatalogFileStore._coerce_material_code(code, used_codes)
            if any(str(template.material) == code for template in templates) and not label:
                raise ValueError("Материал используется в анализах")
            original = current_by_code.get(code)
            used_codes.add(code)
            used_labels.add(label_key)
            normalized.append(
                LabMaterialDTO(
                    code=code,
                    label=label,
                    built_in=bool(raw.get("built_in", code in LAB_MATERIAL_LABELS)),
                    version=int(raw.get("version") or (original.version if original else 1) or 1),
                    created_at=str(raw.get("created_at") or (original.created_at if original else _now_text())),
                    updated_at=_now_text(),
                    last_modified_by=str(raw.get("last_modified_by") or "doctor"),
                )
            )
        removed_codes = {material.code for material in current_materials} - {material.code for material in normalized}
        used_removed = [template.name for template in templates if str(template.material) in removed_codes]
        if used_removed:
            raise ValueError("Материал используется в анализах: " + ", ".join(used_removed[:5]))
        self.file_store.save_catalog(templates, normalized, next_id=self.file_store.next_id(payload, templates))

    def create_template(
        self,
        *,
        name: str,
        material: str = LabMaterial.VENOUS_BLOOD.value,
        comment: str = "",
        default_times: Any = None,
        code: str = "",
    ) -> int:
        if self.settings_service is not None:
            return self.settings_service.create_lab_template(
                name=name,
                material=material,
                comment=comment,
                default_times=default_times,
                code=code,
            )
        payload, templates, materials = self.file_store.load_catalog()
        material_labels = _material_label_map(materials)
        new_id = self.file_store.next_id(payload, templates)
        used_codes = {item.code for item in templates}
        now = _now_text()
        dto = LabAnalysisTemplateDTO(
            id=new_id,
            code=LabAnalysisCatalogFileStore._coerce_code(code, self._normalize_name(name), new_id, used_codes),
            name=self._normalize_name(name),
            material=normalize_lab_material(material, material_labels),
            comment=str(comment or "").strip(),
            default_times=normalize_lab_times(default_times),
            version=1,
            created_at=now,
            updated_at=now,
        )
        templates.append(dto)
        self.file_store.save_catalog(templates, materials, next_id=new_id + 1)
        return new_id

    def update_template(
        self,
        template_id: int,
        *,
        name: str,
        material: str,
        comment: str = "",
        default_times: Any = None,
        expected_version: Optional[int] = None,
    ) -> None:
        if self.settings_service is not None:
            self.settings_service.update_lab_template(
                template_id,
                name=name,
                material=material,
                comment=comment,
                default_times=default_times,
                expected_version=expected_version,
            )
            return
        payload, templates, materials = self.file_store.load_catalog()
        material_labels = _material_label_map(materials)
        current = self._find_template_in_list(templates, template_id)
        if not current:
            raise ValueError("Шаблон анализа не найден")
        if expected_version is not None and int(current.version or 0) != int(expected_version):
            raise ValueError("Шаблон анализа был изменен другим пользователем")
        updated_template = LabAnalysisTemplateDTO(
            id=int(current.id),
            code=current.code,
            name=self._normalize_name(name),
            material=normalize_lab_material(material, material_labels),
            comment=str(comment or "").strip(),
            default_times=normalize_lab_times(default_times),
            version=int(current.version or 0) + 1,
            created_at=current.created_at,
            updated_at=_now_text(),
            last_modified_by="doctor",
        )
        updated = [updated_template if int(item.id) == int(template_id) else item for item in templates]
        self.file_store.save_catalog(updated, materials, next_id=self.file_store.next_id(payload, templates))

    def delete_template(self, template_id: int, *, expected_version: Optional[int] = None) -> None:
        if self.settings_service is not None:
            self.settings_service.delete_lab_template(template_id, expected_version=expected_version)
            return
        payload, templates, materials = self.file_store.load_catalog()
        current = self._find_template_in_list(templates, template_id)
        if not current:
            raise ValueError("Шаблон анализа не найден")
        if expected_version is not None and int(current.version or 0) != int(expected_version):
            raise ValueError("Шаблон анализа был изменен другим пользователем")
        remaining = [item for item in templates if int(item.id) != int(template_id)]
        self.file_store.save_catalog(remaining, materials, next_id=self.file_store.next_id(payload, templates))

    def reorder_templates(self, ordered_template_ids: list[int]) -> None:
        if self.settings_service is not None:
            self.settings_service.reorder_lab_templates(ordered_template_ids)
            return
        payload, templates, materials = self.file_store.load_catalog()
        templates_by_id = {int(item.id): item for item in templates}
        ordered_ids: list[int] = []
        for raw_id in ordered_template_ids or []:
            try:
                template_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if template_id in templates_by_id and template_id not in ordered_ids:
                ordered_ids.append(template_id)

        missing_ids = [int(item.id) for item in templates if int(item.id) not in ordered_ids]
        if not ordered_ids and templates:
            raise ValueError("Не указан порядок шаблонов анализов")

        reordered = [templates_by_id[template_id] for template_id in ordered_ids + missing_ids]
        self.file_store.save_catalog(reordered, materials, next_id=self.file_store.next_id(payload, templates))

    @staticmethod
    def _normalize_name(name: Any) -> str:
        normalized = str(name or "").strip()
        if not normalized:
            raise ValueError("Укажите название анализа")
        return normalized

    @staticmethod
    def _find_template_in_list(
        templates: list[LabAnalysisTemplateDTO],
        template_id: int,
    ) -> Optional[LabAnalysisTemplateDTO]:
        for template in templates:
            if int(template.id) == int(template_id):
                return template
        return None
