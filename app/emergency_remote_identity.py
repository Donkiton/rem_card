from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from rem_card.app.emergency_metadata import EmergencySessionMetadata


@dataclass(frozen=True)
class EmergencyRemoteIdentity:
    medical_db_path: str
    fingerprint_path: str = ""


def normalize_remote_identity_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(str(path or ""))))


def remote_identity_paths_match(base_path: str, remote_path: str) -> bool:
    if not base_path or not remote_path:
        return False
    return normalize_remote_identity_path(base_path) == normalize_remote_identity_path(remote_path)


def build_remote_identity(path: str, fingerprint: dict[str, Any] | None = None) -> EmergencyRemoteIdentity:
    payload = dict(fingerprint or {})
    return EmergencyRemoteIdentity(
        medical_db_path=normalize_remote_identity_path(path),
        fingerprint_path=normalize_remote_identity_path(str(payload.get("path") or "")) if payload.get("path") else "",
    )


def validate_remote_identity_error(
    session: EmergencySessionMetadata,
    remote_path: str,
    validation: Any | None = None,
) -> str:
    fingerprint = dict(getattr(validation, "fingerprint", {}) or {})
    if validation is not None and getattr(validation, "ok", False) and not fingerprint:
        return "remote identity uncertain: remote fingerprint is empty"

    base_fingerprint = dict(session.base_remote_fingerprint or {})
    base_path = str(session.base_remote_db_path or base_fingerprint.get("path") or "").strip()
    if not base_path:
        return "remote identity uncertain: base remote path is missing"

    if not remote_identity_paths_match(base_path, remote_path):
        return f"remote identity mismatch: {base_path} != {remote_path}"

    fingerprint_path = str(base_fingerprint.get("path") or "").strip()
    if fingerprint_path and not remote_identity_paths_match(fingerprint_path, remote_path):
        return f"remote identity mismatch: fingerprint path {fingerprint_path} != {remote_path}"

    return ""
