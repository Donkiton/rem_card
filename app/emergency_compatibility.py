from __future__ import annotations

from typing import Any

from rem_card.app.startup_db_guard import _compare_client_versions
from rem_card.app.version import APP_VERSION


SUPPORTED_EMERGENCY_METADATA_SCHEMA_VERSION = 1


def emergency_metadata_compatibility_error(
    metadata: Any,
    *,
    current_app_version: str = APP_VERSION,
    supported_metadata_schema_version: int = SUPPORTED_EMERGENCY_METADATA_SCHEMA_VERSION,
) -> str:
    try:
        metadata_schema_version = int(getattr(metadata, "metadata_schema_version", 1) or 1)
    except (TypeError, ValueError):
        return "emergency metadata schema version is invalid"
    if metadata_schema_version > int(supported_metadata_schema_version or 1):
        return "emergency metadata schema version is newer than current client"

    metadata_app_version = str(getattr(metadata, "app_version", "") or "").strip()
    if metadata_app_version and _compare_client_versions(str(current_app_version or ""), metadata_app_version) < 0:
        return "emergency metadata app_version is newer than current client"
    return ""


def emergency_metadata_compatible(metadata: Any, *, current_app_version: str = APP_VERSION) -> bool:
    return not emergency_metadata_compatibility_error(metadata, current_app_version=current_app_version)
