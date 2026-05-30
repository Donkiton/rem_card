from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Any

from rem_card.app.db_runtime_context import DbRuntimeContext
from rem_card.app.emergency_metadata import (
    EmergencyMetadataError,
    EmergencySessionMetadata,
    EmergencyStandbyMetadata,
)
from rem_card.app.emergency_paths import (
    active_dir,
    active_session_metadata_path,
    resolve_emergency_root,
)
from rem_card.app.emergency_standby import EmergencyStandbyManager
from rem_card.app.emergency_store import EmergencyLocalStore, EmergencyStoreError
from rem_card.app.emergency_validation import (
    validate_medical_db_snapshot,
    validate_settings_db_snapshot,
)
from rem_card.app.local_metrics import record_metric
from rem_card.app.version import APP_VERSION


EMERGENCY_WORKSTATION_ALLOW_FILE_NAME = "emergency_workstation.allow"
EMERGENCY_WORKSTATION_ALLOW_ENV = "REMCARD_EMERGENCY_WORKSTATION_ALLOWED"

DOCTOR_NETWORK_UNAVAILABLE_MESSAGE = (
    "Сетевая база RemCard недоступна.\n\n"
    "Работа должна быть продолжена на ПК медсестры в аварийном режиме.\n"
    "Не запускайте отдельную локальную копию на этом компьютере."
)

NURSE_EMERGENCY_OFFER_MESSAGE = (
    "Сетевая база RemCard недоступна.\n\n"
    "RemCard может открыть аварийный режим на локальной базе этого ПК.\n\n"
    "До восстановления доступа к сетевой базе работа должна вестись только здесь.\n"
    "Сообщите врачу, что он должен работать на этом компьютере вместе с медсестрой.\n\n"
    "Открыть аварийный режим?"
)

NOT_AUTHORIZED_MESSAGE = (
    "Сетевая база RemCard недоступна.\n\n"
    "Этот ПК не разрешен для аварийного режима RemCard. RemCard будет закрыт."
)

NO_VALID_STANDBY_MESSAGE = (
    "Сетевая база RemCard недоступна.\n\n"
    "На этом ПК нет проверенной аварийной копии базы. RemCard будет закрыт."
)

ACTIVE_SESSION_INVALID_MESSAGE = (
    "Сетевая база RemCard недоступна.\n\n"
    "Локальная аварийная сессия на этом ПК повреждена или неполная. RemCard будет закрыт."
)


@dataclass(frozen=True)
class EmergencyStartupDecision:
    role: str | None
    allowed: bool
    status: str
    user_message: str
    root: str = ""
    technical_reason: str = ""
    standby_metadata: EmergencyStandbyMetadata | None = None
    active_session_metadata: EmergencySessionMetadata | None = None


@dataclass(frozen=True)
class EmergencyStartupSession:
    metadata: EmergencySessionMetadata
    runtime_context: DbRuntimeContext
    resumed: bool


def emergency_workstation_marker_path(root: str | None = None) -> str:
    return os.path.join(resolve_emergency_root(root), EMERGENCY_WORKSTATION_ALLOW_FILE_NAME)


def is_authorized_emergency_workstation(root: str | None = None) -> bool:
    override = str(os.environ.get(EMERGENCY_WORKSTATION_ALLOW_ENV, "")).strip().lower()
    if override in {"1", "true", "yes", "y", "on", "да"}:
        return True
    if override in {"0", "false", "no", "n", "off", "нет"}:
        return False
    return os.path.isfile(emergency_workstation_marker_path(root))


def record_emergency_startup_metric(name: str, value: Any = True, **fields: Any) -> None:
    record_metric(str(name), value, force_flush=True, **fields)


def _failure_text(failure: object) -> str:
    values = [str(failure or "")]
    for attr in ("technical_reason", "user_message", "reason"):
        try:
            value = getattr(failure, attr)
        except Exception:
            value = ""
        if value:
            values.append(str(value))
    return "\n".join(values).lower()


def is_corruption_or_incompatible_startup_error(failure: object) -> bool:
    text = _failure_text(failure)
    markers = (
        "database disk image is malformed",
        "file is not a database",
        "database corruption",
        "malformed",
        "not a database",
        "quick_check failed",
        "integrity_check failed",
        "schema mismatch",
        "schema version is newer",
        "schema incompatible",
        "min_client_version",
        "client_policy",
        "версия программы устарела",
        "версия бд настроек новее",
        "профиль доступа к базе",
        "поврежден",
        "повреждена",
        "несовместим",
        "схема бд",
    )
    return any(marker in text for marker in markers)


def is_network_unavailable_startup_error(failure: object) -> bool:
    if is_corruption_or_incompatible_startup_error(failure):
        return False
    text = _failure_text(failure)
    markers = (
        "database is locked",
        "database table is locked",
        "busy",
        "locked",
        "timeout",
        "could not acquire",
        "unable to open database file",
        "cannot open",
        "database file does not exist",
        "does not exist",
        "not found",
        "no such file",
        "path not found",
        "path inaccessible",
        "network",
        "remote",
        "device is not ready",
        "disk i/o error",
        "input/output error",
        "permission denied",
        "access is denied",
        "readonly database",
        "read-only",
        "папка базы недоступна",
        "база временно недоступна",
        "база данных недоступна",
        "путь к папке базы недоступен",
        "не удалось подготовить защитный контур",
        "не удалось проверить базу данных",
        "не удается найти",
        "системе не удается",
        "отказано в доступе",
        "недоступ",
        "сетев",
        "занят",
        "заблок",
    )
    return any(marker in text for marker in markers)


def classify_startup_failure(failure: object) -> str:
    if is_corruption_or_incompatible_startup_error(failure):
        return "corruption_or_incompatible"
    if is_network_unavailable_startup_error(failure):
        return "network_unavailable"
    return "unknown"


def _standby_metadata_matches_files(metadata: EmergencyStandbyMetadata, result) -> tuple[bool, str]:
    medical_validation = result.medical_validation
    settings_validation = result.settings_validation
    if medical_validation is None or not medical_validation.ok:
        return False, "medical standby validation failed"
    if metadata.medical_db_hash and metadata.medical_db_hash != medical_validation.file_hash:
        return False, "medical standby hash mismatch"
    if int(metadata.medical_db_size or 0) != int(medical_validation.file_size or 0):
        return False, "medical standby size mismatch"
    if int(metadata.schema_version or 0) != int(medical_validation.schema_version or 0):
        return False, "medical standby schema mismatch"
    if str(metadata.app_version or "") != str(APP_VERSION):
        return False, "standby app version mismatch"
    if metadata.settings_db_path:
        if settings_validation is None or not settings_validation.ok:
            return False, "settings standby validation failed"
        if metadata.settings_db_hash and metadata.settings_db_hash != settings_validation.file_hash:
            return False, "settings standby hash mismatch"
        if metadata.settings_db_size is not None and int(metadata.settings_db_size or 0) != int(settings_validation.file_size or 0):
            return False, "settings standby size mismatch"
    return True, "ok"


def validate_active_session_for_startup(metadata: EmergencySessionMetadata) -> tuple[bool, str]:
    if metadata.status not in {"active", "merge_failed"}:
        return False, f"session status is not resumable: {metadata.status}"
    medical_validation = validate_medical_db_snapshot(metadata.local_db_path)
    if not medical_validation.ok:
        return False, f"local emergency DB validation failed: {medical_validation.reason}"
    if not metadata.settings_snapshot_path:
        return False, "settings snapshot path is missing"
    settings_validation = validate_settings_db_snapshot(metadata.settings_snapshot_path)
    if not settings_validation.ok:
        return False, f"settings snapshot validation failed: {settings_validation.reason}"
    if metadata.base_snapshot_hash:
        base_validation = validate_medical_db_snapshot(metadata.base_snapshot_path)
        if not base_validation.ok:
            return False, f"base snapshot validation failed: {base_validation.reason}"
    if str(metadata.app_version or "") != str(APP_VERSION):
        return False, "active session app version mismatch"
    return True, "ok"


def _iter_active_session_ids(root: str) -> list[str]:
    directory = active_dir(root)
    if not os.path.isdir(directory):
        return []
    session_ids: list[str] = []
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        if os.path.isdir(path) and os.path.isfile(active_session_metadata_path(root, name)):
            session_ids.append(name)
    session_ids.sort(key=lambda item: os.path.getmtime(active_session_metadata_path(root, item)), reverse=True)
    return session_ids


def find_resumable_active_session(store: EmergencyLocalStore) -> tuple[EmergencySessionMetadata | None, str]:
    try:
        session_ids = _iter_active_session_ids(store.resolve_root())
    except OSError as exc:
        return None, f"active session directory unavailable: {exc}"

    for session_id in session_ids:
        try:
            metadata = store.read_active_session(session_id)
        except EmergencyMetadataError as exc:
            return None, f"active session metadata error: {exc}"
        if metadata.status == "merged":
            continue
        if metadata.status not in {"active", "merge_failed"}:
            continue
        ok, reason = validate_active_session_for_startup(metadata)
        if not ok:
            return None, reason
        return metadata, "ok"
    return None, "no resumable active session"


def prepare_emergency_startup(role: str | None, root: str | None = None) -> EmergencyStartupDecision:
    if role != "nurse":
        record_emergency_startup_metric("emergency_startup_doctor_blocked", role=role or "")
        return EmergencyStartupDecision(
            role=role,
            allowed=False,
            status="role_not_allowed",
            user_message=DOCTOR_NETWORK_UNAVAILABLE_MESSAGE,
            root=resolve_emergency_root(root),
            technical_reason="emergency startup is only available for nurse role",
        )

    resolved_root = resolve_emergency_root(root)
    if not is_authorized_emergency_workstation(resolved_root):
        return EmergencyStartupDecision(
            role=role,
            allowed=False,
            status="workstation_not_authorized",
            user_message=NOT_AUTHORIZED_MESSAGE,
            root=resolved_root,
            technical_reason="emergency workstation marker is missing",
        )

    store = EmergencyLocalStore(root=resolved_root, source_role=role)
    active_metadata, active_reason = find_resumable_active_session(store)
    if active_metadata is not None:
        return EmergencyStartupDecision(
            role=role,
            allowed=True,
            status="active_session_available",
            user_message=NURSE_EMERGENCY_OFFER_MESSAGE,
            root=resolved_root,
            active_session_metadata=active_metadata,
        )
    if active_reason != "no resumable active session":
        record_emergency_startup_metric("emergency_startup_failed", status="active_session_invalid", reason=active_reason)
        return EmergencyStartupDecision(
            role=role,
            allowed=False,
            status="active_session_invalid",
            user_message=ACTIVE_SESSION_INVALID_MESSAGE,
            root=resolved_root,
            technical_reason=active_reason,
        )

    manager = EmergencyStandbyManager(root=resolved_root, store=store)
    standby_status = manager.validate_standby()
    if not standby_status.ok or standby_status.metadata is None:
        reason = standby_status.reason or "no valid standby"
        record_emergency_startup_metric("emergency_startup_no_valid_standby", reason=reason)
        return EmergencyStartupDecision(
            role=role,
            allowed=False,
            status="no_valid_standby",
            user_message=NO_VALID_STANDBY_MESSAGE,
            root=resolved_root,
            technical_reason=reason,
        )

    metadata_ok, metadata_reason = _standby_metadata_matches_files(standby_status.metadata, standby_status)
    if not metadata_ok:
        record_emergency_startup_metric("emergency_startup_no_valid_standby", reason=metadata_reason)
        return EmergencyStartupDecision(
            role=role,
            allowed=False,
            status="no_valid_standby",
            user_message=NO_VALID_STANDBY_MESSAGE,
            root=resolved_root,
            technical_reason=metadata_reason,
        )

    return EmergencyStartupDecision(
        role=role,
        allowed=True,
        status="standby_available",
        user_message=NURSE_EMERGENCY_OFFER_MESSAGE,
        root=resolved_root,
        standby_metadata=standby_status.metadata,
    )


def start_or_resume_emergency_session(
    decision: EmergencyStartupDecision,
    *,
    root: str | None = None,
    startup_request: dict[str, Any] | None = None,
) -> EmergencyStartupSession:
    if not decision.allowed:
        raise EmergencyStoreError(f"Emergency startup is not allowed: {decision.status}")

    store = EmergencyLocalStore(root=resolve_emergency_root(root or decision.root or None), source_role=decision.role or "nurse")
    resumed = decision.active_session_metadata is not None
    if decision.active_session_metadata is not None:
        metadata = decision.active_session_metadata
    elif decision.standby_metadata is not None:
        metadata = store.create_active_session_from_standby(decision.standby_metadata)
    else:
        raise EmergencyStoreError("Нет standby metadata для запуска аварийного режима")

    if startup_request:
        metadata = replace(
            metadata,
            stale_gap_detected=bool(startup_request.get("stale_gap_detected")),
            standby_last_change_id=int(
                startup_request.get("standby_last_change_id")
                if startup_request.get("standby_last_change_id") is not None
                else metadata.standby_last_change_id or 0
            ),
            last_observed_remote_change_id=int(
                startup_request.get("last_observed_remote_change_id")
                if startup_request.get("last_observed_remote_change_id") is not None
                else metadata.last_observed_remote_change_id or 0
            ),
            outage_detected_at=str(startup_request.get("outage_detected_at") or ""),
        )
        store.write_active_session(metadata)

    ok, reason = validate_active_session_for_startup(metadata)
    if not ok:
        record_emergency_startup_metric("emergency_startup_failed", status="session_validation_failed", reason=reason)
        raise EmergencyStoreError(reason)

    runtime_context = store.build_active_runtime_context(metadata.emergency_session_id)
    record_emergency_startup_metric(
        "emergency_startup_started",
        session_id=metadata.emergency_session_id,
        resumed=resumed,
    )
    record_emergency_startup_metric(
        "emergency_mode_active",
        session_id=metadata.emergency_session_id,
        settings_readonly=runtime_context.settings_readonly,
    )
    return EmergencyStartupSession(metadata=metadata, runtime_context=runtime_context, resumed=resumed)
