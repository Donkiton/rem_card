ROLE_DOCTOR = "doctor"
ROLE_NURSE = "nurse"
ROLE_OPERBLOCK = "operblock"
ROLE_OPERBLOCK_EMERGENCY = "operblock_emergency"
ROLE_OPERBLOCK_PLANNED = "operblock_planned"

OPERBLOCK_ROLE_KEYS = (ROLE_OPERBLOCK, ROLE_OPERBLOCK_EMERGENCY, ROLE_OPERBLOCK_PLANNED)
ROLE_KEYS = (ROLE_DOCTOR, ROLE_NURSE, *OPERBLOCK_ROLE_KEYS)

OPERBLOCK_ROLE_TABLE_CODES = {
    ROLE_OPERBLOCK_EMERGENCY: "emergency",
    ROLE_OPERBLOCK_PLANNED: "planned",
}

ROLE_DISPLAY_NAMES = {
    ROLE_DOCTOR: "Врач",
    ROLE_NURSE: "Медсестра",
    ROLE_OPERBLOCK: "Оперблок",
    ROLE_OPERBLOCK_EMERGENCY: "Экстренная операционная",
    ROLE_OPERBLOCK_PLANNED: "Плановая операционная",
}


def normalize_role_key(role: str | None) -> str:
    return str(role or "").strip().lower()


def is_operblock_role(role: str | None) -> bool:
    return normalize_role_key(role) in OPERBLOCK_ROLE_KEYS


def operblock_table_code_for_role(role: str | None) -> str | None:
    return OPERBLOCK_ROLE_TABLE_CODES.get(normalize_role_key(role))


def role_display_name(role: str | None) -> str:
    role_key = normalize_role_key(role)
    return ROLE_DISPLAY_NAMES.get(role_key, str(role or ""))
