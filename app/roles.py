ROLE_DOCTOR = "doctor"
ROLE_NURSE = "nurse"
ROLE_OPERBLOCK = "operblock"

ROLE_KEYS = (ROLE_DOCTOR, ROLE_NURSE, ROLE_OPERBLOCK)

ROLE_DISPLAY_NAMES = {
    ROLE_DOCTOR: "Врач",
    ROLE_NURSE: "Медсестра",
    ROLE_OPERBLOCK: "Оперблок",
}


def role_display_name(role: str | None) -> str:
    role_key = str(role or "").strip().lower()
    return ROLE_DISPLAY_NAMES.get(role_key, str(role or ""))
