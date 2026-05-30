from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from rem_card.app.emergency_merge_dry_run import EmergencyMergeDryRunService
from rem_card.app.emergency_merge_mode_a import EmergencyModeAMergeService
from rem_card.app.emergency_metadata import EmergencyMetadataError
from rem_card.app.emergency_paths import active_dir, active_session_metadata_path
from rem_card.app.emergency_restore_probe import merge_ready_marker_path
from rem_card.app.emergency_store import EmergencyLocalStore


@dataclass(frozen=True)
class PendingEmergencyMergeResult:
    attempted: bool
    ok: bool
    session_id: str = ""
    dry_run_report_path: str = ""
    merge_report_path: str = ""
    user_message: str = ""
    error: str = ""
    details: dict[str, Any] | None = None


def find_pending_emergency_merge_session(store: EmergencyLocalStore) -> str:
    root = store.resolve_root()
    directory = active_dir(root)
    if not os.path.isdir(directory):
        return ""
    candidates: list[tuple[float, str]] = []
    for name in os.listdir(directory):
        metadata_path = active_session_metadata_path(root, name)
        if not os.path.isfile(metadata_path):
            continue
        try:
            metadata = store.read_active_session(name)
        except EmergencyMetadataError:
            continue
        if metadata.status == "merge_pending":
            candidates.append((os.path.getmtime(metadata_path), metadata.emergency_session_id))
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    return candidates[0][1]


def run_pending_emergency_merge(
    *,
    root: str | None = None,
    source_medical_db_path: str | None = None,
    source_settings_db_path: str | None = None,
    network_baza_dir: str | None = None,
) -> PendingEmergencyMergeResult:
    store = EmergencyLocalStore(root=root)
    session_id = find_pending_emergency_merge_session(store)
    if not session_id:
        return PendingEmergencyMergeResult(attempted=False, ok=True)

    marker_path = merge_ready_marker_path(store.resolve_root(), session_id)
    runtime_context = store.build_active_runtime_context(session_id)
    try:
        dry_run = EmergencyMergeDryRunService(
            role="nurse",
            runtime_context=runtime_context,
            store=store,
            source_medical_db_path=source_medical_db_path,
            source_settings_db_path=source_settings_db_path,
            network_baza_dir=network_baza_dir,
        ).run_dry_run(session_id, marker_path)
        if not dry_run.ok:
            store.mark_session_status(session_id, "merge_failed", dry_run.user_message)
            return PendingEmergencyMergeResult(
                attempted=True,
                ok=False,
                session_id=session_id,
                dry_run_report_path=dry_run.report_path,
                user_message=dry_run.user_message,
                error="dry_run_failed",
                details=dry_run.to_dict(),
            )
        merge = EmergencyModeAMergeService(
            role="nurse",
            runtime_context=runtime_context,
            store=store,
            source_medical_db_path=source_medical_db_path,
            source_settings_db_path=source_settings_db_path,
            network_baza_dir=network_baza_dir,
        ).run_merge(session_id, dry_run.report_path, marker_path)
        return PendingEmergencyMergeResult(
            attempted=True,
            ok=bool(merge.ok),
            session_id=session_id,
            dry_run_report_path=merge.dry_run_report_path or dry_run.report_path,
            merge_report_path=merge.report_path,
            user_message=merge.user_message,
            error=merge.error or merge.error_code,
            details=merge.to_dict(),
        )
    except Exception as exc:
        try:
            store.mark_session_status(session_id, "merge_failed", str(exc))
        except Exception:
            pass
        return PendingEmergencyMergeResult(
            attempted=True,
            ok=False,
            session_id=session_id,
            user_message="Аварийное объединение не завершено. Аварийная база сохранена.",
            error=str(exc),
        )
