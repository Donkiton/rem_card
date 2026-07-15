from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any

from rem_card.app.db_runtime_context import DbRuntimeContext
from rem_card.app.emergency_password_storage import (
    DEFAULT_EMERGENCY_PASSWORD,
    create_emergency_password_record,
    is_emergency_password_record,
    verify_emergency_password_record,
)
from rem_card.data.settings.settings_db import SettingsDatabase
from rem_card.services.settings.settings_service import (
    EMERGENCY_PASSWORD_CHANGE_REQUIRED_KEY,
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
        return SettingsService(
            SettingsDatabase(
                settings_db_path=settings_db_path,
                readonly=True if readonly is None else readonly,
            )
        )
    if runtime_context is not None or readonly is not None:
        return get_settings_service(runtime_context=runtime_context, readonly=readonly)
    return get_settings_service()


def _get_emergency_password_record(service: SettingsService) -> Any:
    return service.get_app_setting(
        EMERGENCY_PASSWORD_SCOPE,
        EMERGENCY_PASSWORD_KEY,
        default=None,
    )


def _read_emergency_password_change_required(service: SettingsService) -> bool:
    return bool(
        service.get_app_setting(
            EMERGENCY_PASSWORD_SCOPE,
            EMERGENCY_PASSWORD_CHANGE_REQUIRED_KEY,
            default=False,
        )
    )


def is_emergency_password_change_required(
    settings_service: SettingsService | None = None,
    *,
    runtime_context: DbRuntimeContext | None = None,
    settings_db_path: str | None = None,
    readonly: bool | None = None,
) -> bool:
    service = _resolve_settings_service(
        settings_service,
        runtime_context=runtime_context,
        settings_db_path=settings_db_path,
        readonly=readonly,
    )
    required = _read_emergency_password_change_required(service)
    stored_value = _get_emergency_password_record(service)
    if isinstance(stored_value, str) and not service.db.settings_readonly:
        try:
            legacy_password = validate_emergency_password_value(stored_value)
            service.set_emergency_password_config(
                create_emergency_password_record(legacy_password),
                change_required=required,
                operation="migrate_legacy",
                changed_by_role="system",
            )
        except Exception:
            # Migration is best-effort and must not block access to settings.
            pass
    return required


def _verify_stored_value(candidate: str, stored_value: Any) -> tuple[bool, bool]:
    if is_emergency_password_record(stored_value):
        return verify_emergency_password_record(candidate, stored_value), False
    if isinstance(stored_value, str):
        try:
            legacy_password = validate_emergency_password_value(stored_value)
        except ValueError:
            return False, False
        return hmac.compare_digest(candidate, legacy_password), True
    return False, False


def verify_emergency_password(
    candidate: Any,
    settings_service: SettingsService | None = None,
    *,
    runtime_context: DbRuntimeContext | None = None,
    settings_db_path: str | None = None,
    readonly: bool | None = None,
) -> bool:
    try:
        provided = normalize_emergency_password(candidate)
        service = _resolve_settings_service(
            settings_service,
            runtime_context=runtime_context,
            settings_db_path=settings_db_path,
            readonly=readonly,
        )
        stored_value = _get_emergency_password_record(service)
        verified, is_legacy_plaintext = _verify_stored_value(provided, stored_value)
    except (RuntimeError, ValueError):
        return False
    if not verified:
        return False

    if is_legacy_plaintext and not service.db.settings_readonly:
        try:
            service.set_emergency_password_config(
                create_emergency_password_record(provided),
                change_required=_read_emergency_password_change_required(service),
                operation="migrate_legacy",
                changed_by_role="system",
            )
        except Exception:
            # A failed opportunistic migration must not invalidate a correct
            # password. The next successful verification will retry it.
            pass
    return True


def verify_emergency_password_for_offline_startup(
    candidate: Any,
    *,
    settings_db_path: str | None = None,
) -> bool:
    if not settings_db_path:
        return False
    return verify_emergency_password(candidate, settings_db_path=settings_db_path, readonly=True)


def set_emergency_password(
    new_password: Any,
    settings_service: SettingsService | None = None,
    *,
    changed_by_role: str | None = "doctor",
    changed_by_user: str | None = None,
) -> EmergencyPasswordChangeResult:
    password = validate_emergency_password_value(new_password)
    service = _resolve_settings_service(settings_service)
    change_required = is_emergency_password_change_required(service)
    if change_required and hmac.compare_digest(password, DEFAULT_EMERGENCY_PASSWORD):
        raise ValueError("Замените временный пароль 123456 на новый пароль.")

    current = _get_emergency_password_record(service)
    same_password, _legacy = _verify_stored_value(password, current)
    service.set_emergency_password_config(
        create_emergency_password_record(password),
        change_required=False,
        operation="update",
        changed_by_role=changed_by_role,
        changed_by_user=changed_by_user,
    )
    return EmergencyPasswordChangeResult(
        changed=bool(change_required or not same_password),
        length=len(password),
    )
