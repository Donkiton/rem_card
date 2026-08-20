from __future__ import annotations

import json
import os
import tempfile
from dataclasses import MISSING, asdict, dataclass, fields
from typing import Any, TypeVar


EMERGENCY_METADATA_JSON_INDENT = 2

SESSION_STATUSES = {
    "standby",
    "active",
    "merge_pending",
    "merging",
    "merged",
    "merge_failed",
    "archived",
    "discarded",
}


class EmergencyMetadataError(RuntimeError):
    pass


@dataclass(frozen=True)
class EmergencySessionMetadata:
    emergency_session_id: str
    status: str
    created_at: str
    started_at: str | None
    ended_at: str | None
    merged_at: str | None
    source_machine: str
    source_windows_user: str
    source_client_id: str
    source_role: str
    app_version: str
    schema_version: int
    base_remote_db_path: str
    base_remote_fingerprint: dict[str, Any]
    base_last_change_id: int
    base_snapshot_hash: str
    base_snapshot_created_at: str
    standby_last_change_id: int | None
    last_observed_remote_change_id: int | None
    local_db_path: str
    base_snapshot_path: str
    settings_snapshot_path: str | None
    merge_attempt_count: int
    last_merge_error: str | None
    validation_status: str
    validation_error: str | None
    stale_gap_detected: bool = False
    outage_detected_at: str | None = None
    last_dry_run_at: str | None = None
    last_dry_run_status: str | None = None
    last_dry_run_report_path: str | None = None
    last_dry_run_mode: str | None = None
    last_dry_run_error: str | None = None
    last_merge_report_path: str | None = None
    remote_backup_path: str | None = None
    local_backup_path: str | None = None
    merge_result: str | None = None
    final_remote_last_change_id: int | None = None
    final_remote_hash: str | None = None
    discarded_at: str | None = None
    discard_reason: str | None = None
    discard_report_path: str | None = None
    metadata_schema_version: int = 1

    def __post_init__(self) -> None:
        _validate_status(self.status)


@dataclass(frozen=True)
class EmergencyStandbyMetadata:
    standby_id: str
    created_at: str
    updated_at: str
    source_remote_db_path: str
    source_remote_fingerprint: dict[str, Any]
    source_settings_db_path: str | None
    source_settings_fingerprint: dict[str, Any] | None
    remote_last_change_id: int
    schema_version: int
    app_version: str
    medical_db_path: str
    medical_db_hash: str
    medical_db_size: int
    medical_db_mtime: float
    settings_db_path: str | None
    settings_db_hash: str | None
    settings_db_size: int | None
    settings_db_mtime: float | None
    quick_check_status: str
    settings_quick_check_status: str | None
    validation_status: str
    validation_error: str | None
    metadata_schema_version: int = 1
    generation_id: str = ""
    generation_dir: str = ""


T = TypeVar("T", EmergencySessionMetadata, EmergencyStandbyMetadata)


def _validate_status(status: str) -> None:
    if status not in SESSION_STATUSES:
        raise EmergencyMetadataError(f"Недопустимый статус emergency-сессии: {status}")


def _coerce_dataclass(cls: type[T], payload: dict[str, Any]) -> T:
    allowed = {field.name for field in fields(cls)}
    filtered = {key: value for key, value in payload.items() if key in allowed}
    missing = []
    for field in fields(cls):
        if field.name in filtered:
            continue
        if field.default is not MISSING:
            filtered[field.name] = field.default
            continue
        if field.default_factory is not MISSING:  # type: ignore[attr-defined]
            filtered[field.name] = field.default_factory()  # type: ignore[misc]
            continue
        missing.append(field.name)
    if missing:
        raise EmergencyMetadataError(f"Metadata не содержит обязательные поля: {', '.join(missing)}")
    try:
        return cls(**filtered)
    except EmergencyMetadataError:
        raise
    except Exception as exc:
        raise EmergencyMetadataError(f"Некорректная metadata: {exc}") from exc


def session_metadata_from_dict(payload: dict[str, Any]) -> EmergencySessionMetadata:
    return _coerce_dataclass(EmergencySessionMetadata, payload)


def standby_metadata_from_dict(payload: dict[str, Any]) -> EmergencyStandbyMetadata:
    return _coerce_dataclass(EmergencyStandbyMetadata, payload)


def metadata_to_dict(metadata: EmergencySessionMetadata | EmergencyStandbyMetadata) -> dict[str, Any]:
    return asdict(metadata)


def atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    base_name = os.path.basename(path)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{base_name}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=EMERGENCY_METADATA_JSON_INDENT, sort_keys=True)
            fh.write("\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp_path, path)
        tmp_path = ""
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def read_json_file(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except FileNotFoundError as exc:
        raise EmergencyMetadataError(f"Metadata не найдена: {path}") from exc
    except json.JSONDecodeError as exc:
        raise EmergencyMetadataError(f"Metadata повреждена: {path} ({exc})") from exc
    except OSError as exc:
        raise EmergencyMetadataError(f"Metadata недоступна: {path} ({exc})") from exc
    if not isinstance(payload, dict):
        raise EmergencyMetadataError(f"Metadata должна быть JSON-объектом: {path}")
    return payload
