from __future__ import annotations

import re
from typing import Any


PROFILE_DEPARTMENTS: tuple[str, ...] = (
    "Терапия",
    "Хирургия",
    "Травматология",
    "Гинекология",
    "Неврология",
    "Кардиология",
    "Инфекционно-педиатрическое",
)

_LEGACY_OPERBLOCK_DEPARTMENT_NAMES = {
    "оперблок",
    "операционный блок",
    "экстренная операционная",
    "плановая операционная",
}


def normalize_profile_department(value: Any, *, clear_legacy_operblock: bool = False) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if clear_legacy_operblock and text.casefold().replace("ё", "е") in _LEGACY_OPERBLOCK_DEPARTMENT_NAMES:
        return ""
    return text
