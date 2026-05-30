from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rem_card.app.db_runtime_context import DbRuntimeContext
from rem_card.data.settings.settings_db import SettingsDatabase
from rem_card.services.settings.settings_service import (
    DEFAULT_EMERGENCY_PASSWORD,
    EMERGENCY_PASSWORD_CATALOG_KEY,
    EMERGENCY_PASSWORD_KEY,
    MIN_EMERGENCY_PASSWORD_LENGTH,
    SettingsService,
    get_settings_service,
)


EMERGENCY_PASSWORD_SCOPE = "shared"


@dataclass(frozen=True)
class EmergencyPasswordChangeResult:
    changed: bool
    length: int


def normalize_emergency_password(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Аварийный пароль должен быть строкой")
    return value.strip()


def validate_emergency_password_value(value: Any) -> str:
    password = normalize_emergency_password(value)
    if len(password) < MIN_EMERGENCY_PASSWORD_LENGTH:
        raise ValueError(
            f"Аварийный пароль должен содержать минимум {MIN_EMERGENCY_PASSWORD_LENGTH} символов"
        )
    return password


def _resolve_settings_service(
    settings_service: SettingsService | None = None,
    *,
    runtime_context: DbRuntimeContext | None = None,
    settings_db_path: str | None = None,
    readonly: bool | None = None,
) -> SettingsService:
    if settings_service is not None:
        return settings_service
    if settings_db_path:
        return SettingsService(SettingsDatabase(settings_db_path=settings_db_path, readonly=True if readonly is None else readonly))
    if runtime_context is not None or readonly is not None:
        return get_settings_service(runtime_context=runtime_context, readonly=readonly)
    return get_settings_service()


def get_emergency_password(
    settings_service: SettingsService | None = None,
    *,
    runtime_context: DbRuntimeContext | None = None,
    settings_db_path: str | None = None,
    readonly: bool | None = None,
) -> str:
    service = _resolve_settings_service(
        settings_service,
        runtime_context=runtime_context,
        settings_db_path=settings_db_path,
        readonly=readonly,
    )
    value = service.get_app_setting(
        EMERGENCY_PASSWORD_SCOPE,
        EMERGENCY_PASSWORD_KEY,
        default=None,
    )
    if value is None:
        return DEFAULT_EMERGENCY_PASSWORD
    try:
        return validate_emergency_password_value(value)
    except ValueError as exc:
        raise RuntimeError("Аварийный пароль в БД настроек поврежден") from exc


def verify_emergency_password(
    candidate: Any,
    settings_service: SettingsService | None = None,
    *,
    runtime_context: DbRuntimeContext | None = None,
    settings_db_path: str | None = None,
    readonly: bool | None = None,
) -> bool:
    try:
        expected = get_emergency_password(
            settings_service,
            runtime_context=runtime_context,
            settings_db_path=settings_db_path,
            readonly=readonly,
        )
        provided = normalize_emergency_password(candidate)
    except (RuntimeError, ValueError):
        return False
    return provided == expected


def set_emergency_password(
    new_password: Any,
    settings_service: SettingsService | None = None,
    *,
    changed_by_role: str | None = "doctor",
    changed_by_user: str | None = None,
) -> EmergencyPasswordChangeResult:
    password = validate_emergency_password_value(new_password)
    service = _resolve_settings_service(settings_service)
    current = service.get_app_setting(
        EMERGENCY_PASSWORD_SCOPE,
        EMERGENCY_PASSWORD_KEY,
        default=None,
    )
    if current == password:
        return EmergencyPasswordChangeResult(changed=False, length=len(password))
    service.set_app_setting(
        EMERGENCY_PASSWORD_SCOPE,
        EMERGENCY_PASSWORD_KEY,
        password,
        catalog_key=EMERGENCY_PASSWORD_CATALOG_KEY,
        entity_type="app_settings",
        operation="update",
        changed_by_role=changed_by_role,
        changed_by_user=changed_by_user,
    )
    return EmergencyPasswordChangeResult(changed=True, length=len(password))
