from __future__ import annotations

import os
import json
from dataclasses import dataclass
from typing import Any

from rem_card.app.emergency_metadata import EmergencyMetadataError
from rem_card.app.emergency_paths import active_dir, active_session_metadata_path
from rem_card.app.emergency_store import EmergencyLocalStore

ROW_LEVEL_MERGE_STRATEGY = "row_level"
MERGE_AUTHORIZATION_FILE = "merge_authorization.json"


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


@dataclass(frozen=True)
class PendingEmergencyMergeCandidate:
    session_id: str = ""
    status: str = ""


def find_pending_emergency_merge_candidate(store: EmergencyLocalStore) -> PendingEmergencyMergeCandidate:
    root = store.resolve_root()
    directory = active_dir(root)
    if not os.path.isdir(directory):
        return PendingEmergencyMergeCandidate()
    candidates: list[tuple[int, float, str, str]] = []
    status_priority = {"merging": 3, "merge_pending": 2, "merge_failed": 1}
    for name in os.listdir(directory):
        metadata_path = active_session_metadata_path(root, name)
        if not os.path.isfile(metadata_path):
            continue
        try:
            metadata = store.read_active_session(name)
        except EmergencyMetadataError:
            continue
        status = str(metadata.status or "")
        if status in status_priority:
            candidates.append((status_priority[status], os.path.getmtime(metadata_path), metadata.emergency_session_id, status))
    if not candidates:
        return PendingEmergencyMergeCandidate()
    candidates.sort(reverse=True)
    _priority, _mtime, session_id, status = candidates[0]
    return PendingEmergencyMergeCandidate(session_id=session_id, status=status)


def find_pending_emergency_merge_session(store: EmergencyLocalStore) -> str:
    candidate = find_pending_emergency_merge_candidate(store)
    return candidate.session_id if candidate.status in {"merge_pending", "merging", "merge_failed"} else ""


def load_merge_authorization(store: EmergencyLocalStore, session_id: str) -> dict[str, Any] | None:
    """Read a UI-approved selection without changing the emergency session."""
    path = os.path.join(active_dir(store.resolve_root()), session_id, MERGE_AUTHORIZATION_FILE)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    selected = payload.get("selected_admission_ids") if isinstance(payload, dict) else None
    digest = payload.get("plan_digest") if isinstance(payload, dict) else None
    try:
        schema_version = int(payload.get("schema_version") or 0) if isinstance(payload, dict) else 0
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(payload, dict)
        or schema_version != 1
        or str(payload.get("session_id") or "") != session_id
        or not isinstance(selected, list)
        or not selected
        or not isinstance(digest, str)
        or not digest
    ):
        return None
    try:
        payload["selected_admission_ids"] = [int(value) for value in selected]
    except (TypeError, ValueError):
        return None
    return payload


def run_pending_emergency_merge(
    *, root: str | None = None, source_medical_db_path: str | None = None,
    source_settings_db_path: str | None = None, network_baza_dir: str | None = None,
) -> PendingEmergencyMergeResult:
    from rem_card.app.emergency_participants import EmergencySessionBusy, exclusive_emergency_merge

    store = EmergencyLocalStore(root=root)
    candidate = find_pending_emergency_merge_candidate(store)
    if not candidate.session_id:
        return PendingEmergencyMergeResult(attempted=False, ok=True)
    directory = os.path.join(active_dir(store.resolve_root()), candidate.session_id)
    try:
        with exclusive_emergency_merge(directory):
            return _run_pending_emergency_merge(
                root=root, source_medical_db_path=source_medical_db_path,
                source_settings_db_path=source_settings_db_path, network_baza_dir=network_baza_dir,
            )
    except EmergencySessionBusy as exc:
        return PendingEmergencyMergeResult(attempted=False, ok=False, session_id=candidate.session_id,
                                           error="local_session_busy", user_message=str(exc),
                                           details={"requires_recovery": False})


def _run_pending_emergency_merge(
    *,
    root: str | None = None,
    source_medical_db_path: str | None = None,
    source_settings_db_path: str | None = None,
    network_baza_dir: str | None = None,
) -> PendingEmergencyMergeResult:
    store = EmergencyLocalStore(root=root)
    candidate = find_pending_emergency_merge_candidate(store)
    session_id = candidate.session_id
    if not session_id:
        return PendingEmergencyMergeResult(attempted=False, ok=True)
    authorization = load_merge_authorization(store, session_id)
    if authorization is None:
        if candidate.status == "merge_failed":
            return PendingEmergencyMergeResult(
                attempted=False,
                ok=False,
                session_id=session_id,
                user_message="Предыдущее аварийное объединение завершилось ошибкой. Аварийная база сохранена; требуется повторная проверка или сопровождение.",
                error="merge_failed_unresolved",
                details={"status": candidate.status, "commit_outcome_known": False, "requires_recovery": True},
            )
        return PendingEmergencyMergeResult(
            attempted=False,
            ok=False,
            session_id=session_id,
            user_message="Аварийное объединение ожидает просмотра и подтверждения выбранных карт.",
            error="review_required",
            details={"status": candidate.status, "review_required": True},
        )

    from rem_card.app.emergency_merge_dry_run import EmergencyMergeDryRunService
    from rem_card.app.emergency_restore_probe import merge_ready_marker_path

    from rem_card.app.emergency_row_level_merge import EmergencyRowLevelMergeService as MergeService

    marker_path = merge_ready_marker_path(store.resolve_root(), session_id)
    runtime_context = store.build_active_runtime_context(session_id)
    try:
        if candidate.status in {"merging", "merge_failed"}:
            from rem_card.app.emergency_row_level_merge import EmergencyRowLevelMergeService

            merge = EmergencyRowLevelMergeService(
                role="nurse", runtime_context=runtime_context, store=store,
                source_medical_db_path=source_medical_db_path,
                source_settings_db_path=source_settings_db_path,
                network_baza_dir=network_baza_dir,
            ).run_merge(
                session_id,
                selected_admission_ids=authorization["selected_admission_ids"],
                expected_plan_digest=authorization["plan_digest"],
            )
            details = _merge_details_with_commit_state(merge)
            return PendingEmergencyMergeResult(
                attempted=True, ok=bool(merge.ok), session_id=session_id,
                dry_run_report_path=merge.dry_run_report_path,
                merge_report_path=merge.report_path, user_message=merge.user_message,
                error=merge.error or merge.error_code, details=details,
            )
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
                details={**dry_run.to_dict(), "commit_outcome_known": True, "requires_recovery": False},
            )
        merge = MergeService(
            role="nurse",
            runtime_context=runtime_context,
            store=store,
            source_medical_db_path=source_medical_db_path,
            source_settings_db_path=source_settings_db_path,
            network_baza_dir=network_baza_dir,
        ).run_merge(
            session_id, dry_run.report_path, marker_path,
            selected_admission_ids=authorization["selected_admission_ids"],
            expected_plan_digest=authorization["plan_digest"],
        )
        return PendingEmergencyMergeResult(
            attempted=True,
            ok=bool(merge.ok),
            session_id=session_id,
            dry_run_report_path=merge.dry_run_report_path or dry_run.report_path,
            merge_report_path=merge.report_path,
            user_message=merge.user_message,
            error=merge.error or merge.error_code,
            details=_merge_details_with_commit_state(merge),
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
            details={"commit_outcome_known": False, "requires_recovery": True},
        )


def _merge_details_with_commit_state(merge: Any) -> dict[str, Any]:
    details = dict(merge.to_dict())
    code = str(merge.error_code or "")
    rollback_restored = str(merge.rollback_status or "") == "restored"
    no_commit_codes = {
        "review_required", "prerequisites_blocked", "locks_unavailable", "active_session_lock",
        "row_merge_plan_blocked", "plan_digest_mismatch", "remote_invalid",
    }
    receipt_needs_recovery = code == "receipt_validation_failed"
    known = bool(merge.ok) or rollback_restored or code in no_commit_codes or receipt_needs_recovery
    details["commit_outcome_known"] = known
    details["requires_recovery"] = receipt_needs_recovery or not known
    explicit_recovery = getattr(merge, "requires_recovery", None)
    if explicit_recovery is not None:
        details["requires_recovery"] = bool(explicit_recovery)
        details["commit_outcome_known"] = known or not explicit_recovery
    return details
