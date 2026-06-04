from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable


OPERBLOCK_ICONS_KEY = "operblock_icons"
MAX_OPERBLOCK_ICON_BLOB_BYTES = 32 * 1024 * 1024
SUPPORTED_OPERBLOCK_ICON_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".svg"}


@dataclass(frozen=True)
class OperBlockIconDefinition:
    icon_key: str
    category: str
    target_key: str
    name: str
    default_file: str
    sort_order: int = 0
    description: str = ""
    source_file: str = ""


TYPE_ICON_DEFINITIONS: tuple[OperBlockIconDefinition, ...] = (
    OperBlockIconDefinition(
        "type:bolus",
        "type",
        "bolus",
        "Иконка болюса в блоке назначений",
        "bolus.png",
        10,
    ),
    OperBlockIconDefinition(
        "type:timed_infusion",
        "type",
        "timed_infusion",
        "Иконка капельницы в блоке назначений",
        "kapelnitsa.png",
        20,
    ),
    OperBlockIconDefinition(
        "type:continuous_infusion",
        "type",
        "continuous_infusion",
        "Иконка дозатора в блоке назначений",
        "perfuzor.png",
        30,
    ),
    OperBlockIconDefinition(
        "type:gas",
        "type",
        "gas",
        "Иконка газа в блоке назначений",
        "gas.png",
        40,
    ),
)


EDIT_ICON_DEFINITIONS: tuple[OperBlockIconDefinition, ...] = (
    OperBlockIconDefinition(
        "edit:bolus",
        "edit",
        "bolus",
        "Иконка болюса в окне изменения",
        "bolus-izm.png",
        110,
    ),
    OperBlockIconDefinition(
        "edit:timed_infusion",
        "edit",
        "timed_infusion",
        "Иконка капельницы в окне изменения",
        "kapeln_izm.png",
        120,
    ),
    OperBlockIconDefinition(
        "edit:continuous_infusion",
        "edit",
        "continuous_infusion",
        "Иконка дозатора в окне изменения",
        "dozator_izm.png",
        130,
    ),
    OperBlockIconDefinition(
        "edit:gas",
        "edit",
        "gas",
        "Иконка газа в окне изменения",
        "gas_izm.png",
        140,
    ),
)


DEFAULT_DRUG_ICON_FILES = {
    "bolus": "bolus-izm.png",
    "timed_infusion": "kapeln_izm.png",
    "continuous_infusion": "dozator_izm.png",
    "gas": "gas_izm.png",
}


SEEDED_CUSTOM_ICON_DEFINITIONS: tuple[OperBlockIconDefinition, ...] = (
    OperBlockIconDefinition(
        "drug:manual:gas:sevoflurane",
        "drug",
        "manual:gas:sevoflurane",
        "Иконка препарата: Севофлюран",
        "gas_izm.png",
        10010,
        source_file="sevodrag.png",
    ),
    OperBlockIconDefinition(
        "drug:gas:sevoflurane",
        "drug",
        "gas:sevoflurane",
        "Иконка препарата: Севофлюран",
        "gas_izm.png",
        10011,
        source_file="sevodrag.png",
    ),
    OperBlockIconDefinition(
        "drug-source:sevoflurane",
        "drug",
        "sevoflurane",
        "Иконка препарата: Севофлюран",
        "gas_izm.png",
        10012,
        source_file="sevodrag.png",
    ),
)


DRUG_ICON_LABEL_ALIAS_KEYS: dict[str, tuple[str, ...]] = {
    "sevoflurane": ("drug:manual:gas:sevoflurane", "drug:gas:sevoflurane", "drug-source:sevoflurane"),
    "севофлюран": ("drug:manual:gas:sevoflurane", "drug:gas:sevoflurane", "drug-source:sevoflurane"),
    "sevoran": ("drug:manual:gas:sevoflurane", "drug:gas:sevoflurane", "drug-source:sevoflurane"),
    "севоран": ("drug:manual:gas:sevoflurane", "drug:gas:sevoflurane", "drug-source:sevoflurane"),
}


DEFAULT_ICON_DEFINITIONS: tuple[OperBlockIconDefinition, ...] = (
    *TYPE_ICON_DEFINITIONS,
    *EDIT_ICON_DEFINITIONS,
)

DEFAULT_ICON_DEFINITION_BY_KEY = {
    definition.icon_key: definition for definition in DEFAULT_ICON_DEFINITIONS
}


def _clean_kind(kind: Any) -> str:
    text = str(kind or "").strip().casefold()
    aliases = {
        "continuous": "continuous_infusion",
        "dozator": "continuous_infusion",
        "дозатор": "continuous_infusion",
        "infusion": "timed_infusion",
        "капельница": "timed_infusion",
        "gas": "gas",
        "газ": "gas",
        "bolus": "bolus",
        "болюс": "bolus",
    }
    return aliases.get(text, text or "bolus")


def type_icon_key(kind: Any) -> str:
    return f"type:{_clean_kind(kind)}"


def edit_icon_key(kind: Any) -> str:
    return f"edit:{_clean_kind(kind)}"


def default_icon_file_for_key(icon_key: Any, fallback_file: str = "") -> str:
    definition = DEFAULT_ICON_DEFINITION_BY_KEY.get(str(icon_key or "").strip())
    return definition.default_file if definition else str(fallback_file or "").strip()


def default_drug_icon_file(kind: Any) -> str:
    return DEFAULT_DRUG_ICON_FILES.get(_clean_kind(kind), DEFAULT_DRUG_ICON_FILES["bolus"])


def slug_for_icon_key(value: Any) -> str:
    text = str(value or "").strip().casefold().replace("ё", "е")
    text = re.sub(r"[^0-9a-zа-я_:-]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_:")
    return text or "item"


def drug_icon_key_for_identity(
    *,
    preset_id: Any = None,
    source_drug_id: Any = None,
    label: Any = None,
) -> str:
    preset_text = str(preset_id or "").strip()
    if preset_text:
        return f"drug:{preset_text}"
    source_text = str(source_drug_id or "").strip()
    if source_text:
        return f"drug-source:{source_text}"
    return f"drug-label:{slug_for_icon_key(label)}"


def drug_icon_candidate_keys(
    *,
    preset_id: Any = None,
    source_drug_id: Any = None,
    label: Any = None,
) -> list[str]:
    result: list[str] = []
    def append(key: str) -> None:
        if key and key not in result and not key.endswith(":item") and not key.endswith(":"):
            result.append(key)

    for key in (
        drug_icon_key_for_identity(preset_id=preset_id),
        drug_icon_key_for_identity(source_drug_id=source_drug_id),
        drug_icon_key_for_identity(label=label),
    ):
        append(key)
    for alias_key in DRUG_ICON_LABEL_ALIAS_KEYS.get(slug_for_icon_key(label), ()):
        append(alias_key)
    return result


def drug_icon_candidate_keys_from_payload(payload: dict[str, Any] | None, label: Any = None) -> list[str]:
    data = payload if isinstance(payload, dict) else {}
    result: list[str] = []

    def extend(keys: Iterable[str]) -> None:
        for key in keys:
            text = str(key or "").strip()
            if text and text not in result:
                result.append(text)

    extend(
        drug_icon_candidate_keys(
            preset_id=data.get("preset_id"),
            source_drug_id=data.get("source_drug_id"),
        )
    )
    for label_value in (
        data.get("display_name"),
        data.get("label"),
        data.get("latin"),
        data.get("drug_label"),
        data.get("display_label"),
        label,
    ):
        extend(drug_icon_candidate_keys(label=label_value))
    return result


def first_icon_key(keys: str | Iterable[str]) -> str:
    if isinstance(keys, str):
        return keys
    for key in keys:
        text = str(key or "").strip()
        if text:
            return text
    return ""
