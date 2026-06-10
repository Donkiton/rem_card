from __future__ import annotations

from dataclasses import dataclass
from typing import Any


REMCARD_ICON_STORAGE_CATALOG_KEY = "operblock_icons"
REMCARD_EMPTY_BED_ICON_KEY = "remcard:patient_preview:empty_bed"
REMCARD_MALE_PATIENT_ICON_KEY = "remcard:patient_preview:male"
REMCARD_FEMALE_PATIENT_ICON_KEY = "remcard:patient_preview:female"


@dataclass(frozen=True)
class RemCardIconDefinition:
    icon_key: str
    category: str
    target_key: str
    name: str
    default_file: str
    sort_order: int = 0
    description: str = ""
    source_file: str = ""


REMCARD_ICON_DEFINITIONS: tuple[RemCardIconDefinition, ...] = (
    RemCardIconDefinition(
        REMCARD_EMPTY_BED_ICON_KEY,
        "remcard_patient_preview",
        "empty_bed",
        "Фото свободной койки",
        "bad.png",
        20100,
        "Показывается в предпросмотре карточки пациента, когда койка свободна.",
        source_file="bad.png",
    ),
    RemCardIconDefinition(
        REMCARD_MALE_PATIENT_ICON_KEY,
        "remcard_patient_preview",
        "male_patient",
        "Фото пациента: мужчина",
        "man_in_oper_extr.png",
        20110,
        "Показывается в предпросмотре карточки пациента для мужского пола.",
        source_file="man_in_oper_extr.png",
    ),
    RemCardIconDefinition(
        REMCARD_FEMALE_PATIENT_ICON_KEY,
        "remcard_patient_preview",
        "female_patient",
        "Фото пациента: женщина",
        "woman_in_oper_extr.png",
        20120,
        "Показывается в предпросмотре карточки пациента для женского пола.",
        source_file="woman_in_oper_extr.png",
    ),
)

REMCARD_ICON_DEFINITION_BY_KEY = {
    definition.icon_key: definition for definition in REMCARD_ICON_DEFINITIONS
}


def default_remcard_icon_file_for_key(icon_key: Any, fallback_file: str = "") -> str:
    definition = REMCARD_ICON_DEFINITION_BY_KEY.get(str(icon_key or "").strip())
    return definition.default_file if definition else str(fallback_file or "").strip()
