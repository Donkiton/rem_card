from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from copy import deepcopy
import json
import os
import re
import tempfile
from typing import Any, Mapping

from rem_card.app.paths import SEED_DIR, USER_DICT_DIR
from rem_card.services.operblock_quick_orders import (
    build_operblock_quick_order_text,
    load_operblock_quick_orders,
    normalize_operblock_quick_order_kind,
)


OPERBLOCK_MEDICATION_PRESETS_FILE = "operblock_medication_presets.seed.json"
OPERBLOCK_MEDICATION_PRESETS_OVERRIDE_KEY = "operblock_medication_presets"
OPERBLOCK_MEDICATION_PRESETS_VERSION = 1
DRUGS_SEED_FILE = "drugs.seed.json"
DILUENTS_SEED_FILE = "diluents.seed.json"

OPERBLOCK_MEDICATION_PRESET_KINDS = {
    "bolus",
    "gas",
    "continuous_infusion",
    "timed_infusion",
}

LEGACY_PRESET_KIND_MAP = {
    "infusion": "continuous_infusion",
    "continuous": "continuous_infusion",
    "continuous_infusion": "continuous_infusion",
    "dozator": "continuous_infusion",
    "дозатор": "continuous_infusion",
    "перфузор": "continuous_infusion",
    "перфузоры": "continuous_infusion",
    "инфузия": "continuous_infusion",
    "инфузии": "continuous_infusion",
    "инфузии / перфузор": "continuous_infusion",
    "timed": "timed_infusion",
    "timed_infusion": "timed_infusion",
    "капельница": "timed_infusion",
    "капельницы": "timed_infusion",
    "solvent": "timed_infusion",
    "раствор": "timed_infusion",
    "растворы": "timed_infusion",
    "event": "bolus",
    "событие": "bolus",
    "bolus": "bolus",
    "болюс": "bolus",
    "болюсы": "bolus",
    "gas": "gas",
    "газ": "gas",
    "газы": "gas",
    "ингаляция": "gas",
    "ингаляции": "gas",
    "inhalation": "gas",
}

ADMIN_TYPE_KIND_MAP = {
    "bolus": "bolus",
    "gas": "gas",
    "inhalation": "gas",
    "infusion": "timed_infusion",
    "continuous": "continuous_infusion",
    "dozator": "continuous_infusion",
}

GROUP_LABELS = {
    "vasopressors": "Вазопрессоры",
    "sedatives": "Седация",
    "relaxants": "Миорелаксанты",
    "narcotic_analgesics": "Анальгезия",
    "analgesics": "Анальгезия",
    "antibiotics": "Антибиотики / капельницы",
    "fluids": "Растворы / прочее",
}

KIND_GROUP_LABELS = {
    "bolus": "Болюсы",
    "gas": "Газы",
    "continuous_infusion": "Дозатор",
    "timed_infusion": "Капельницы",
}

UNIT_LABELS = {
    "g": "г",
    "mg": "мг",
    "mcg": "мкг",
    "mkg": "мкг",
    "ug": "мкг",
    "ml": "мл",
    "iu": "ЕД",
    "ед": "ЕД",
}

KNOWN_PRESET_FIELDS = {
    "preset_id",
    "source_drug_id",
    "label",
    "drug_name",
    "drug",
    "display_name",
    "latin",
    "aliases",
    "kind",
    "group",
    "route",
    "unit",
    "default_dose",
    "doses",
    "rates",
    "rate_unit",
    "concentration",
    "concentration_text",
    "solvent_id",
    "solvent_label",
    "solvent_volume_ml",
    "duration_min",
    "card_color",
    "card_color_hex",
    "color",
    "uses_line",
    "enabled",
    "favorite",
    "is_favorite",
    "pinned",
    "sort_order",
    "payload",
}


@dataclass(frozen=True)
class OperBlockMedicationPreset:
    preset_id: str
    source_drug_id: str | None
    label: str
    display_name: str
    latin: str | None = None
    aliases: list[str] = field(default_factory=list)
    kind: str = "bolus"
    group: str = "Болюсы"
    route: str | None = None
    unit: str | None = None
    default_dose: str | None = None
    doses: list[str] = field(default_factory=list)
    rates: list[str] = field(default_factory=list)
    rate_unit: str | None = None
    concentration: str | None = None
    solvent_id: str | None = None
    solvent_label: str | None = None
    solvent_volume_ml: str | None = None
    duration_min: int | None = None
    card_color: str | None = None
    uses_line: bool = False
    enabled: bool = False
    favorite: bool = False
    sort_order: int | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["payload"] = dict(self.payload or {})
        return data


def _read_json_dict(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _apply_dictionary_overrides(base: Mapping[str, Any], overrides: Any) -> dict[str, Any]:
    result = dict(base or {})
    if not isinstance(overrides, Mapping):
        return result
    for key, value in overrides.items():
        if isinstance(value, Mapping) and value.get("_deleted"):
            result.pop(key, None)
        else:
            result[key] = value
    return result


def _write_json_atomic(path: str, payload: dict[str, Any]) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".operblock_medication_presets_", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _as_optional_text(value: Any) -> str | None:
    text = _as_text(value)
    return text or None


def operblock_medication_preset_display_name(preset: Mapping[str, Any]) -> str:
    return _as_text(
        preset.get("display_name")
        or preset.get("label")
        or preset.get("drug_name")
        or preset.get("drug")
        or preset.get("latin")
    )


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().casefold() in {"1", "true", "yes", "да", "on"}


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_hex_color(value: Any) -> str | None:
    text = _as_text(value)
    if not text:
        return None
    if re.fullmatch(r"#[0-9A-Fa-f]{6}", text):
        return text.lower()
    return None


def _preset_color_value(data: Mapping[str, Any]) -> Any:
    for key in ("card_color", "card_color_hex", "color"):
        if key in data:
            return data.get(key)
    return None


def _as_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = _as_text(item)
        if text and text not in result:
            result.append(text)
    return result


def _format_decimal(value: Any) -> str:
    try:
        number = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return _as_text(value)
    normalized = number.normalize()
    if normalized == normalized.to_integral():
        return str(normalized.quantize(Decimal("1")))
    return format(normalized, "f").rstrip("0").rstrip(".").replace(".", ",")


def _format_unit(unit: Any) -> str:
    raw = _as_text(unit).casefold()
    return UNIT_LABELS.get(raw, _as_text(unit))


def _dose_option(value: Any, unit: Any) -> str:
    clean_value = _format_decimal(value)
    clean_unit = _format_unit(unit)
    return f"{clean_value} {clean_unit}".strip() if clean_value and clean_unit else clean_value


def _dose_options_from_drug(raw: Mapping[str, Any]) -> list[str]:
    unit = raw.get("unit")
    values: list[Any] = []
    if isinstance(raw.get("forms"), list):
        values.extend(raw.get("forms") or [])
    if raw.get("default_dose") not in values:
        values.append(raw.get("default_dose"))
    options: list[str] = []
    for value in values:
        text = _dose_option(value, unit)
        if text and text not in options:
            options.append(text)
    return options


def _normalize_kind(value: Any) -> str:
    kind = _as_text(value).casefold()
    return LEGACY_PRESET_KIND_MAP.get(kind, "bolus")


def normalize_operblock_medication_preset_kind(value: Any) -> str:
    return _normalize_kind(value)


def _slug(value: Any) -> str:
    text = _as_text(value).casefold()
    text = re.sub(r"[^0-9a-zа-яё]+", "_", text, flags=re.IGNORECASE).strip("_")
    return text or "preset"


def _label_key(value: Any) -> str:
    return re.sub(r"\s+", " ", _as_text(value).casefold())


def _default_group(kind: str, source_group: Any = None) -> str:
    source_key = _as_text(source_group).casefold()
    if source_key in GROUP_LABELS:
        return GROUP_LABELS[source_key]
    return KIND_GROUP_LABELS.get(kind, "Болюсы")


def _diluent_label(diluents: Mapping[str, Any], diluent_id: Any) -> str | None:
    key = _as_text(diluent_id)
    raw = diluents.get(key)
    if isinstance(raw, Mapping):
        return _as_optional_text(raw.get("display") or raw.get("latin") or raw.get("label"))
    return None


def project_drug_to_operblock_preset(
    drug_id: str,
    raw: Mapping[str, Any],
    *,
    diluents: Mapping[str, Any] | None = None,
    enabled: bool = False,
    preset_id: str | None = None,
) -> dict[str, Any]:
    admin_type = _as_text(raw.get("admin_type")).casefold()
    kind = ADMIN_TYPE_KIND_MAP.get(admin_type, "bolus")
    latin = _as_optional_text(raw.get("latin"))
    label = latin or drug_id
    display_name = _as_text(raw.get("display_name") or raw.get("display") or label)
    default_dilution = raw.get("default_dilution") if isinstance(raw.get("default_dilution"), Mapping) else {}
    solvent_id = _as_optional_text(default_dilution.get("base"))
    solvent_label = _diluent_label(diluents or {}, solvent_id)
    preset = OperBlockMedicationPreset(
        preset_id=preset_id or f"drug:{drug_id}",
        source_drug_id=drug_id,
        label=label,
        display_name=display_name,
        latin=latin,
        aliases=_as_list(raw.get("aliases")),
        kind=kind,
        group=_default_group(kind, raw.get("group")),
        unit=_as_optional_text(raw.get("unit")),
        default_dose=_dose_option(raw.get("default_dose"), raw.get("unit")) if raw.get("default_dose") is not None else None,
        doses=_dose_options_from_drug(raw),
        solvent_id=solvent_id,
        solvent_label=solvent_label,
        solvent_volume_ml=_as_optional_text(default_dilution.get("volume")),
        duration_min=_as_int(raw.get("duration_min")),
        uses_line=_as_bool(raw.get("uses_line")),
        enabled=enabled,
        payload={"source": "drugs.seed.json", "source_group": raw.get("group")},
    )
    return preset.to_dict()


def _normalize_preset(raw: Mapping[str, Any], *, base: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    data = dict(base or {})
    data.update(dict(raw or {}))
    label = _as_text(data.get("label") or data.get("drug_name") or data.get("drug") or data.get("latin"))
    if not label:
        return None
    kind = _normalize_kind(data.get("kind"))
    preset_id = _as_text(data.get("preset_id")) or f"manual:{kind}:{_slug(label)}"
    display_name = _as_text(data.get("display_name") or label)
    payload = dict(data.get("payload") or {}) if isinstance(data.get("payload"), Mapping) else {}
    extra_fields = {
        key: value
        for key, value in data.items()
        if key not in KNOWN_PRESET_FIELDS and value not in (None, "", [])
    }
    if extra_fields:
        existing_extra = payload.get("extra_fields") if isinstance(payload.get("extra_fields"), Mapping) else {}
        merged_extra = dict(existing_extra)
        merged_extra.update(extra_fields)
        payload["extra_fields"] = merged_extra
    preset = OperBlockMedicationPreset(
        preset_id=preset_id,
        source_drug_id=_as_optional_text(data.get("source_drug_id")),
        label=label,
        display_name=display_name,
        latin=_as_optional_text(data.get("latin")),
        aliases=_as_list(data.get("aliases")),
        kind=kind,
        group=_as_text(data.get("group")) or _default_group(kind),
        route=_as_optional_text(data.get("route")),
        unit=_as_optional_text(data.get("unit")),
        default_dose=_as_optional_text(data.get("default_dose")),
        doses=_as_list(data.get("doses")),
        rates=_as_list(data.get("rates")),
        rate_unit=_as_optional_text(data.get("rate_unit")),
        concentration=_as_optional_text(data.get("concentration") or data.get("concentration_text")),
        solvent_id=_as_optional_text(data.get("solvent_id")),
        solvent_label=_as_optional_text(data.get("solvent_label")),
        solvent_volume_ml=_as_optional_text(data.get("solvent_volume_ml")),
        duration_min=_as_int(data.get("duration_min")),
        card_color=_as_hex_color(_preset_color_value(data)),
        uses_line=_as_bool(data.get("uses_line")),
        enabled=_as_bool(data.get("enabled"), default=False),
        favorite=_as_bool(data.get("favorite") or data.get("is_favorite") or data.get("pinned"), default=False),
        sort_order=_as_int(data.get("sort_order")),
        payload=dict(payload),
    )
    return preset.to_dict()


def _merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in overlay.items():
        if key == "payload":
            payload = dict(result.get("payload") or {})
            if isinstance(value, Mapping):
                payload.update(dict(value))
            result["payload"] = payload
            continue
        result[key] = value
    return result


def _load_seed_items(
    seed_dir: str,
    *,
    user_dict_dir: str | None = None,
    overrides_data: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    if overrides_data is None:
        resolved_user_dir = user_dict_dir or USER_DICT_DIR
        overrides_data = _read_json_dict(os.path.join(resolved_user_dir, "user_overrides.json"))
    drugs = _apply_dictionary_overrides(
        _read_json_dict(os.path.join(seed_dir, DRUGS_SEED_FILE)),
        (overrides_data or {}).get("drugs"),
    )
    diluents = _apply_dictionary_overrides(
        _read_json_dict(os.path.join(seed_dir, DILUENTS_SEED_FILE)),
        (overrides_data or {}).get("diluents"),
    )
    opblock_payload = _read_json_dict(os.path.join(seed_dir, OPERBLOCK_MEDICATION_PRESETS_FILE))
    raw_items = opblock_payload.get("items") if isinstance(opblock_payload.get("items"), list) else []
    return drugs, diluents, list(raw_items)


def load_operblock_diluent_options(
    *,
    seed_dir: str | None = None,
    user_dict_dir: str | None = None,
) -> list[dict[str, Any]]:
    resolved_seed_dir = seed_dir or SEED_DIR
    resolved_user_dir = user_dict_dir or USER_DICT_DIR
    overrides = _read_json_dict(os.path.join(resolved_user_dir, "user_overrides.json"))
    diluents = _apply_dictionary_overrides(
        _read_json_dict(os.path.join(resolved_seed_dir, DILUENTS_SEED_FILE)),
        overrides.get("diluents"),
    )
    options: list[dict[str, Any]] = []
    for diluent_id, raw in sorted(diluents.items(), key=lambda item: _label_key(_diluent_label(diluents, item[0]) or item[0])):
        if not isinstance(raw, Mapping):
            continue
        label = _diluent_label(diluents, diluent_id) or _as_text(diluent_id)
        if not label:
            continue
        options.append(
            {
                "id": _as_text(diluent_id),
                "label": label,
                "latin": _as_optional_text(raw.get("latin")),
                "display": _as_optional_text(raw.get("display")),
            }
        )
    return options


def _apply_opblock_seed(
    presets: dict[str, dict[str, Any]],
    drugs: Mapping[str, Any],
    diluents: Mapping[str, Any],
    raw_items: list[dict[str, Any]],
) -> None:
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            continue
        source_drug_id = _as_text(raw_item.get("source_drug_id"))
        preset_id = _as_text(raw_item.get("preset_id"))
        base = None
        if source_drug_id and isinstance(drugs.get(source_drug_id), Mapping):
            base = project_drug_to_operblock_preset(
                source_drug_id,
                drugs[source_drug_id],
                diluents=diluents,
                enabled=False,
                preset_id=preset_id or None,
            )
        normalized = _normalize_preset(raw_item, base=base)
        if not normalized:
            continue
        normalized["enabled"] = _as_bool(raw_item.get("enabled"), default=True)
        normalized.setdefault("payload", {})
        normalized["payload"] = _merge(normalized.get("payload") or {}, {"opblock_seed": True})
        presets[normalized["preset_id"]] = normalized


def quick_order_to_medication_preset(raw: Mapping[str, Any], *, sort_order: int | None = None) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    label = _as_text(raw.get("label") or raw.get("drug_name") or raw.get("drug"))
    if not label:
        return None
    quick_kind = normalize_operblock_quick_order_kind(raw.get("kind"))
    if quick_kind == "infusion":
        kind = "continuous_infusion"
    elif quick_kind == "gas":
        kind = "gas"
    else:
        kind = "bolus"
    preset = _normalize_preset(
        {
            "preset_id": f"quick:{kind}:{_slug(label)}",
            "label": label,
            "display_name": _as_text(raw.get("display_name") or label),
            "kind": kind,
            "group": KIND_GROUP_LABELS.get(kind, "Болюсы"),
            "doses": _as_list(raw.get("doses")),
            "rates": _as_list(raw.get("rates")),
            "concentration": raw.get("concentration") or raw.get("concentration_text"),
            "enabled": True,
            "favorite": _as_bool(raw.get("favorite") or raw.get("is_favorite") or raw.get("pinned"), default=False),
            "sort_order": sort_order,
            "card_color": raw.get("card_color") or raw.get("card_color_hex") or raw.get("color"),
            "payload": {"compat_source": "operblock_quick_orders", "legacy_group": raw.get("group")},
        }
    )
    return preset


def _apply_quick_orders_compat(presets: dict[str, dict[str, Any]], quick_items: list[dict[str, Any]]) -> None:
    label_index = {_label_key(item.get("label")): preset_id for preset_id, item in presets.items()}
    for index, quick_item in enumerate(quick_items):
        preset = quick_order_to_medication_preset(quick_item, sort_order=10_000 + index)
        if not preset:
            continue
        existing_id = label_index.get(_label_key(preset.get("label")))
        if existing_id:
            existing = dict(presets[existing_id])
            for key in ("doses", "rates", "concentration"):
                value = preset.get(key)
                if value:
                    existing[key] = value
            existing["enabled"] = True
            existing["payload"] = _merge(existing.get("payload") or {}, preset.get("payload") or {})
            presets[existing_id] = _normalize_preset(existing) or existing
            continue
        presets[preset["preset_id"]] = preset
        label_index[_label_key(preset.get("label"))] = preset["preset_id"]


def _apply_overrides(presets: dict[str, dict[str, Any]], raw_items: list[Any]) -> None:
    seen: set[str] = set()
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            continue
        preset_id = _as_text(raw_item.get("preset_id"))
        if _is_disabled_base_drug_preset(raw_item):
            if preset_id:
                seen.add(preset_id)
            continue
        base = presets.get(preset_id) if preset_id else None
        normalized = _normalize_preset(raw_item, base=base)
        if not normalized:
            continue
        seen.add(normalized["preset_id"])
        presets[normalized["preset_id"]] = normalized
    for preset_id in list(presets):
        if preset_id.startswith("manual:") and preset_id not in seen:
            presets.pop(preset_id, None)


def _is_disabled_base_drug_preset(preset: Mapping[str, Any]) -> bool:
    preset_id = _as_text(preset.get("preset_id"))
    if not preset_id.startswith("drug:"):
        return False
    if _as_bool(preset.get("enabled"), default=False):
        return False
    return bool(_as_text(preset.get("source_drug_id")) or preset_id.startswith("drug:"))


def _sort_key(preset: Mapping[str, Any]) -> tuple[int, int, str, str]:
    kind_order = {
        "bolus": 0,
        "gas": 1,
        "continuous_infusion": 2,
        "timed_infusion": 3,
        "solvent": 4,
        "event": 5,
    }.get(str(preset.get("kind") or ""), 9)
    sort_order = _as_int(preset.get("sort_order"))
    return (
        kind_order,
        sort_order if sort_order is not None else 99_999,
        _label_key(preset.get("display_name") or preset.get("label")),
        _as_text(preset.get("preset_id")),
    )


def normalize_operblock_medication_presets_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        payload = {}
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_item in payload.get("items") or []:
        if not isinstance(raw_item, Mapping):
            continue
        normalized = _normalize_preset(raw_item)
        if not normalized:
            continue
        if _is_disabled_base_drug_preset(normalized):
            continue
        preset_id = normalized["preset_id"]
        if preset_id in seen:
            continue
        seen.add(preset_id)
        items.append(normalized)
    items.sort(key=_sort_key)
    return {"version": OPERBLOCK_MEDICATION_PRESETS_VERSION, "items": items}


def load_operblock_medication_presets(
    *,
    seed_dir: str | None = None,
    user_dict_dir: str | None = None,
    include_disabled: bool = True,
) -> list[dict[str, Any]]:
    resolved_seed_dir = seed_dir or SEED_DIR
    resolved_user_dir = user_dict_dir or USER_DICT_DIR
    overrides = _read_json_dict(os.path.join(resolved_user_dir, "user_overrides.json"))
    drugs, diluents, opblock_items = _load_seed_items(
        resolved_seed_dir,
        user_dict_dir=resolved_user_dir,
        overrides_data=overrides,
    )

    presets: dict[str, dict[str, Any]] = {}
    for drug_id, raw in drugs.items():
        if isinstance(raw, Mapping):
            preset = project_drug_to_operblock_preset(drug_id, raw, diluents=diluents, enabled=False)
            presets[preset["preset_id"]] = preset

    override_payload = overrides.get(OPERBLOCK_MEDICATION_PRESETS_OVERRIDE_KEY)
    include_opblock_seed = True
    include_quick_orders_compat = True
    if isinstance(override_payload, Mapping):
        include_opblock_seed = _as_bool(override_payload.get("include_opblock_seed"), default=True)
        include_quick_orders_compat = _as_bool(
            override_payload.get("include_quick_orders_compat"),
            default=True,
        )

    if include_opblock_seed:
        _apply_opblock_seed(presets, drugs, diluents, opblock_items)
    if include_quick_orders_compat:
        try:
            quick_items = load_operblock_quick_orders()
        except Exception:
            quick_items = []
        _apply_quick_orders_compat(presets, quick_items)

    if isinstance(override_payload, Mapping):
        raw_override_items = override_payload.get("items") if isinstance(override_payload.get("items"), list) else []
        _apply_overrides(presets, raw_override_items)

    items = [item for item in presets.values() if include_disabled or bool(item.get("enabled"))]
    items.sort(key=_sort_key)
    return [deepcopy(item) for item in items]


def save_operblock_medication_presets(
    items: list[dict[str, Any]],
    *,
    user_dict_dir: str | None = None,
) -> list[dict[str, Any]]:
    payload = normalize_operblock_medication_presets_payload(
        {"version": OPERBLOCK_MEDICATION_PRESETS_VERSION, "items": items}
    )
    resolved_user_dir = user_dict_dir or USER_DICT_DIR
    overrides_path = os.path.join(resolved_user_dir, "user_overrides.json")
    overrides = _read_json_dict(overrides_path)
    existing_payload = overrides.get(OPERBLOCK_MEDICATION_PRESETS_OVERRIDE_KEY)
    if isinstance(existing_payload, Mapping):
        for flag in ("include_opblock_seed", "include_quick_orders_compat"):
            if flag in existing_payload:
                payload[flag] = _as_bool(existing_payload.get(flag), default=True)
    overrides[OPERBLOCK_MEDICATION_PRESETS_OVERRIDE_KEY] = payload
    _write_json_atomic(overrides_path, overrides)
    return list(payload["items"])


def reset_operblock_medication_presets_override(*, user_dict_dir: str | None = None) -> None:
    resolved_user_dir = user_dict_dir or USER_DICT_DIR
    overrides_path = os.path.join(resolved_user_dir, "user_overrides.json")
    overrides = _read_json_dict(overrides_path)
    overrides.pop(OPERBLOCK_MEDICATION_PRESETS_OVERRIDE_KEY, None)
    _write_json_atomic(overrides_path, overrides)


def build_operblock_preset_bolus_text(preset: Mapping[str, Any], dose: str | None = None) -> str:
    display_name = operblock_medication_preset_display_name(preset)
    return build_operblock_quick_order_text(display_name, dose)


def build_operblock_preset_payload(preset: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(preset.get("payload") or {}) if isinstance(preset.get("payload"), Mapping) else {}
    for key in (
        "preset_id",
        "source_drug_id",
        "label",
        "display_name",
        "latin",
        "kind",
        "concentration",
        "solvent_id",
        "solvent_label",
        "solvent_volume_ml",
        "duration_min",
        "card_color",
    ):
        value = preset.get(key)
        if value not in (None, "", []):
            payload[key] = value
    return payload

