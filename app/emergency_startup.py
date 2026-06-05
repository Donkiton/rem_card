from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Any

from rem_card.app.db_access_classifier import classify_database_access_error
from rem_card.app.db_runtime_context import DbRuntimeContext
from rem_card.app.emergency_compatibility import emergency_metadata_compatibility_error
from rem_card.app.emergency_metadata import (
    EmergencyMetadataError,
    EmergencySessionMetadata,
    EmergencyStandbyMetadata,
)
from rem_card.app.emergency_paths import (
    active_dir,
    active_session_metadata_path,
    resolve_emergency_root,
    standby_settings_db_path,
)
from rem_card.app.emergency_standby import EmergencyStandbyManager
from rem_card.app.emergency_store import EmergencyLocalStore, EmergencyStoreError
from rem_card.app.emergency_validation import (
    validate_medical_db_snapshot,
    validate_settings_db_snapshot,
)
from rem_card.app.local_metrics import record_metric


DOCTOR_NETWORK_UNAVAILABLE_MESSAGE = (
    "Сетевая база RemCard недоступна.\n\n"
    "Работа должна быть продолжена на ПК медсестры в аварийном режиме.\n"
    "Не запускайте отдельную локальную копию на этом компьютере."
)

NURSE_EMERGENCY_OFFER_MESSAGE = (
    "Сетевая база RemCard недоступна.\n\n"
    "Для перехода в аварийный режим работы нужно ввести аварийный пароль.\n\n"
    "После подтверждения RemCard откроет локальную аварийную базу этого ПК.\n\n"
    "До восстановления доступа к сетевой базе работа должна вестись только здесь."
)

ACTIVE_SESSION_OFFER_MESSAGE = (
    "Сетевая база RemCard недоступна.\n\n"
    "На этом ПК уже есть активная аварийная сессия RemCard.\n\n"
    "RemCard откроет её на локальной аварийной базе."
)

EMPTY_EMERGENCY_DATABASE_MESSAGE = (
    "Сетевая база RemCard недоступна.\n\n"
    "Для перехода в аварийный режим работы нужно ввести аварийный пароль.\n\n"
    "Проверенная аварийная копия базы на этом ПК не найдена. "
    "После подтверждения RemCard создаст пустую локальную аварийную базу без пациентов."
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
    password_settings_db_path: str = ""
    empty_database_allowed: bool = False
    standby_metadata: EmergencyStandbyMetadata | None = None
    active_session_metadata: EmergencySessionMetadata | None = None


@dataclass(frozen=True)
class EmergencyStartupSession:
    metadata: EmergencySessionMetadata
    runtime_context: DbRuntimeContext
    resumed: bool


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
    category = classify_database_access_error(failure)
    if category == "locked_busy":
        return False
    if category == "network_unavailable":
        return True
    text = _failure_text(failure)
    markers = (
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
    )
    return any(marker in text for marker in markers)


def classify_startup_failure(failure: object) -> str:
    if is_corruption_or_incompatible_startup_error(failure):
        return "corruption_or_incompatible"
    category = classify_database_access_error(failure)
    if category == "locked_busy":
        return "locked_busy"
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
    compatibility_error = emergency_metadata_compatibility_error(metadata)
    if compatibility_error:
        return False, compatibility_error
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
    compatibility_error = emergency_metadata_compatibility_error(metadata)
    if compatibility_error:
        return False, compatibility_error
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


def _standby_failure_allows_empty_database(status: str, reason: str) -> bool:
    normalized_status = str(status or "").strip().lower()
    normalized_reason = str(reason or "").strip().lower()
    if normalized_status in {"metadata_error", "expired"}:
        return True
    markers = (
        "metadata не найдена",
        "metadata not found",
        "file does not exist",
        "does not exist",
        "path is missing",
        "не найд",
        "не существует",
    )
    return any(marker in normalized_reason for marker in markers)


def _valid_standby_settings_path(root: str) -> str:
    path = standby_settings_db_path(root)
    if not os.path.isfile(path):
        return ""
    validation = validate_settings_db_snapshot(path)
    if not validation.ok:
        return ""
    return path


def prepare_emergency_startup(role: str | None, root: str | None = None) -> EmergencyStartupDecision:
    resolved_root = resolve_emergency_root(root)
    store = EmergencyLocalStore(root=resolved_root, source_role=role)
    active_metadata, active_reason = find_resumable_active_session(store)
    if active_metadata is not None:
        return EmergencyStartupDecision(
            role=role,
            allowed=True,
            status="active_session_available",
            user_message=ACTIVE_SESSION_OFFER_MESSAGE,
            root=resolved_root,
            password_settings_db_path=str(active_metadata.settings_snapshot_path or ""),
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

    if role != "nurse":
        record_emergency_startup_metric("emergency_startup_doctor_blocked", role=role or "")
        return EmergencyStartupDecision(
            role=role,
            allowed=False,
            status="role_not_allowed",
            user_message=DOCTOR_NETWORK_UNAVAILABLE_MESSAGE,
            root=resolved_root,
            technical_reason="emergency startup creation is only available for nurse role",
        )

    manager = EmergencyStandbyManager(root=resolved_root, store=store)
    standby_status = manager.validate_standby()
    if not standby_status.ok or standby_status.metadata is None:
        reason = standby_status.reason or "no valid standby"
        record_emergency_startup_metric("emergency_startup_no_valid_standby", reason=reason)
        if _standby_failure_allows_empty_database(standby_status.status, reason):
            return EmergencyStartupDecision(
                role=role,
                allowed=True,
                status="empty_database_available",
                user_message=EMPTY_EMERGENCY_DATABASE_MESSAGE,
                root=resolved_root,
                technical_reason=reason,
                password_settings_db_path=_valid_standby_settings_path(resolved_root),
                empty_database_allowed=True,
            )
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
        if _standby_failure_allows_empty_database(standby_status.status, metadata_reason):
            return EmergencyStartupDecision(
                role=role,
                allowed=True,
                status="empty_database_available",
                user_message=EMPTY_EMERGENCY_DATABASE_MESSAGE,
                root=resolved_root,
                technical_reason=metadata_reason,
                password_settings_db_path=_valid_standby_settings_path(resolved_root),
                empty_database_allowed=True,
            )
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
        password_settings_db_path=str(standby_status.metadata.settings_db_path or ""),
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
    elif decision.empty_database_allowed:
        metadata = store.create_active_session_from_empty_database(
            settings_source_path=decision.password_settings_db_path,
            reason=decision.technical_reason or "standby unavailable",
        )
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


def _network_transition_probe_block_message(status: dict[str, Any]) -> str:
    status_name = str((status or {}).get("status") or "")
    reason = str((status or {}).get("reason") or (status or {}).get("error") or "").strip()
    if status_name == "session_lock_active":
        details = "на сетевой базе открыто другое окно RemCard"
    elif status_name in {"db_lock_active", "emergency_merge_lock_active"}:
        details = "сетевая база сейчас занята служебной операцией"
    elif status_name.startswith("network_"):
        details = "сетевая база пока недоступна"
    else:
        details = "проверка сетевой базы не разрешила объединение"
    if reason:
        details = f"{details}\n\nТехническая причина: {reason}"
    return (
        "Переход на основную БД сейчас невозможен.\n\n"
        f"{details}\n\n"
        "RemCard откроет активную аварийную сессию, чтобы аварийные данные не потерялись."
    )


def mark_active_emergency_session_merge_pending_for_network_start(
    decision: EmergencyStartupDecision,
    *,
    source_medical_db_path: str | None = None,
    source_settings_db_path: str | None = None,
    network_baza_dir: str | None = None,
) -> tuple[bool, str]:
    metadata = decision.active_session_metadata
    if metadata is None:
        return False, "Активная аварийная сессия не найдена."

    from rem_card.app.emergency_restore_probe import EmergencyRestoreProbe

    store = EmergencyLocalStore(root=decision.root or None, source_role="nurse")
    runtime_context = store.build_active_runtime_context(metadata.emergency_session_id)
    probe = EmergencyRestoreProbe(
        role="nurse",
        runtime_context=runtime_context,
        store=store,
        session_metadata=metadata,
        success_rounds_required=1,
        stability_window_sec=1.0,
        source_medical_db_path=source_medical_db_path,
        source_settings_db_path=source_settings_db_path,
        network_baza_dir=network_baza_dir,
        is_shutdown=lambda: False,
        is_local_write_idle=lambda: True,
        is_local_maintenance_idle=lambda: True,
    )
    try:
        status = probe.run_probe_once()
        status_name = str(status.get("status") or "")
        ready = (
            status_name == "merge_ready_mode_a" and bool(status.get("merge_ready"))
        ) or (
            status_name == "remote_changed_conflict_pending" and bool(status.get("network_stable"))
        )
        if not ready:
            return False, _network_transition_probe_block_message(status)
        marker_path = probe.mark_merge_ready()
        record_metric(
            "emergency_startup_network_switch_merge_ready",
            1,
            session_id=metadata.emergency_session_id,
            marker_path=marker_path,
            status=status_name,
        )
        return True, marker_path
    finally:
        probe.release_network_emergency_role_marker()
