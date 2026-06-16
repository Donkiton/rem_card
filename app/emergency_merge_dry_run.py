from __future__ import annotations

import json
import os
import socket
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable

from rem_card.app.db_runtime_context import DbRuntimeContext, build_network_runtime_context
from rem_card.app.emergency_metadata import (
    EmergencyMetadataError,
    EmergencySessionMetadata,
    atomic_write_json,
    read_json_file,
)
from rem_card.app.emergency_paths import active_session_dir
from rem_card.app.emergency_restore_probe import (
    emergency_merge_lock_path,
    merge_ready_marker_path,
)
from rem_card.app.emergency_remote_identity import remote_identity_paths_match, validate_remote_identity_error
from rem_card.app.emergency_store import EmergencyLocalStore
from rem_card.app.emergency_validation import (
    SnapshotValidationResult,
    compute_file_hash,
    validate_medical_db_snapshot,
    validate_settings_db_snapshot,
)
from rem_card.app.sqlite_shared import NETWORK_SAFE_DB_PROFILE, configure_connection
from rem_card.app.startup_db_guard import _compare_client_versions
from rem_card.app.version import APP_VERSION


DRY_RUN_REPORT_VERSION = 1
DEFAULT_MARKER_TTL_SEC = max(60, int(float(os.environ.get("REMCARD_MERGE_READY_MARKER_TTL_SEC", "86400"))))

READY_MODE_A_MESSAGE = (
    "Проверка объединения завершена.\n"
    "Сетевая база не изменялась с момента перехода в аварийный режим.\n"
    "Можно выполнить автоматическое объединение на следующем этапе."
)
REMOTE_CHANGED_AUTHORITATIVE_MESSAGE = (
    "Сетевая база изменилась после создания аварийной копии.\n"
    "Проверка разрешила emergency-authoritative row-level объединение: локальные аварийные изменения "
    "будут перенесены в сетевую медицинскую БД после резервных копий и проверок.\n"
    "При конфликте RemCard-строк победит локальная аварийная версия. Сетевая БД настроек заменяться не будет."
)


class EmergencyMergeMode(str, Enum):
    REMOTE_UNCHANGED_MODE_A = "remote_unchanged_mode_a"
    REMOTE_CHANGED_EMERGENCY_AUTHORITATIVE = "remote_changed_emergency_authoritative"
    REMOTE_INCONSISTENT = "remote_inconsistent"
    UNKNOWN_REMOTE_STATE = "unknown_remote_state"
    BLOCKED = "blocked"


class EmergencyMergeBlocker(str, Enum):
    MARKER_REQUIRED = "marker_required"
    MARKER_INVALID = "marker_invalid"
    ROLE_NOT_ALLOWED = "role_not_allowed"
    RUNTIME_MODE_NOT_ALLOWED = "runtime_mode_not_allowed"
    LOCAL_INVALID = "local_invalid"
    REMOTE_INVALID = "remote_invalid"
    REMOTE_IDENTITY_MISMATCH = "blocked_remote_identity_mismatch"
    LOCKS = "locks"
    BACKUP_NOT_READY = "backup_not_ready"
    INCONSISTENT = "inconsistent"
    REMOTE_CHANGED = "remote_changed"
    FAILED = "failed"


@dataclass(frozen=True)
class EmergencyChangeSummary:
    local_last_change_id: int = 0
    base_last_change_id: int = 0
    emergency_change_count: int = 0
    changed_tables_summary: dict[str, dict[str, int]] = field(default_factory=dict)
    touched_admissions: list[int] = field(default_factory=list)
    touched_patients: list[int] = field(default_factory=list)
    first_emergency_change_at: str | None = None
    last_emergency_change_at: str | None = None
    change_log_note: str = "change_log is index only, not full replay log"
    local_db_hash: str = ""
    local_db_size: int = 0
    local_db_mtime: float = 0.0
    base_snapshot_hash: str = ""
    settings_snapshot_hash: str = ""


@dataclass(frozen=True)
class EmergencyMergeDryRunReport:
    report_version: int
    created_at: str
    emergency_session_id: str
    result_status: str
    merge_mode: str
    blockers: list[dict[str, str]]
    warnings: list[str]
    base_last_change_id: int
    local_last_change_id: int
    remote_last_change_id: int
    base_snapshot_hash: str
    local_db_hash: str
    remote_fingerprint: dict[str, Any]
    settings_snapshot_hash: str
    changed_tables_summary: dict[str, dict[str, int]]
    touched_admissions: list[int]
    touched_patients: list[int]
    locks_status: dict[str, Any]
    backup_readiness_status: dict[str, Any]
    schema_versions: dict[str, int]
    app_version: str
    next_allowed_action: str


@dataclass(frozen=True)
class EmergencyMergeDryRunResult:
    ok: bool
    result_status: str
    merge_mode: str = EmergencyMergeMode.BLOCKED.value
    blockers: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    session_id: str = ""
    marker_path: str = ""
    report_path: str = ""
    user_message: str = ""
    local_validation: dict[str, Any] = field(default_factory=dict)
    remote_validation: dict[str, Any] = field(default_factory=dict)
    locks_status: dict[str, Any] = field(default_factory=dict)
    backup_readiness_status: dict[str, Any] = field(default_factory=dict)
    change_summary: EmergencyChangeSummary = field(default_factory=EmergencyChangeSummary)
    remote_fingerprint: dict[str, Any] = field(default_factory=dict)
    base_last_change_id: int = 0
    local_last_change_id: int = 0
    remote_last_change_id: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return payload


def load_merge_ready_marker(path: str) -> dict[str, Any]:
    payload = read_json_file(path)
    if not isinstance(payload, dict):
        raise EmergencyMetadataError(f"Merge-ready marker должен быть JSON-объектом: {path}")
    return payload


def validate_merge_ready_marker(
    marker: dict[str, Any],
    session: EmergencySessionMetadata,
    *,
    now: datetime | None = None,
    ttl_sec: int = DEFAULT_MARKER_TTL_SEC,
) -> tuple[bool, str]:
    if str(marker.get("emergency_session_id") or "") != session.emergency_session_id:
        return False, "marker session id mismatch"
    if str(marker.get("status") or "") != "merge_ready_requested":
        return False, "marker status is not merge_ready_requested"
    marker_mode = str(marker.get("mode") or "")
    if marker_mode not in {"mode_a_remote_unchanged", "remote_changed_emergency_authoritative"}:
        return False, "marker mode is not allowed"
    if str(marker.get("source_role") or "").lower() != "nurse":
        return False, "marker source role is not nurse"
    base_last = int(session.base_last_change_id or 0)
    if _int_value(marker.get("base_last_change_id"), default=-1) != base_last:
        return False, "marker base_last_change_id mismatch"
    marker_remote_last = _int_value(marker.get("remote_last_change_id"), default=-1)
    if marker_remote_last < base_last:
        return False, "marker remote_last_change_id is below base"
    if marker_mode == "mode_a_remote_unchanged" and marker_remote_last != base_last:
        return False, "mode A marker remote_last_change_id mismatch"
    requested_at = _parse_marker_time(marker.get("requested_at"))
    if requested_at is None:
        return False, "marker requested_at is invalid"
    current = now or datetime.now(timezone.utc)
    if requested_at.tzinfo is None:
        requested_at = requested_at.replace(tzinfo=timezone.utc)
    if current - requested_at > timedelta(seconds=max(1, int(ttl_sec or 1))):
        return False, "marker expired"
    return True, "ok"


class EmergencyMergeDryRunService:
    def __init__(
        self,
        *,
        role: str | None = "nurse",
        runtime_context: DbRuntimeContext | None = None,
        store: EmergencyLocalStore | None = None,
        network_context_factory: Callable[[], DbRuntimeContext] | None = None,
        source_medical_db_path: str | None = None,
        source_settings_db_path: str | None = None,
        network_baza_dir: str | None = None,
        marker_ttl_sec: int = DEFAULT_MARKER_TTL_SEC,
    ):
        self.role = str(role or "").strip().lower()
        self.runtime_context = runtime_context
        self.store = store or EmergencyLocalStore(root=_infer_emergency_root(runtime_context))
        self.network_context_factory = network_context_factory or build_network_runtime_context
        self.source_medical_db_path = _optional_path(source_medical_db_path)
        self.source_settings_db_path = _optional_path(source_settings_db_path)
        self.network_baza_dir = _optional_path(network_baza_dir)
        self.marker_ttl_sec = max(1, int(marker_ttl_sec or 1))

    def run_dry_run(self, session_id: str, marker_path: str | None = None) -> EmergencyMergeDryRunResult:
        session = self._read_session_or_none(session_id)
        result = self._run_dry_run(session_id, marker_path, session)
        report_path = self.write_dry_run_report(result)
        result = replace(result, report_path=report_path)
        if session is not None:
            self._update_session_dry_run_metadata(session, result)
        return result

    def validate_local_emergency_side(self, session: EmergencySessionMetadata) -> tuple[bool, dict[str, Any], list[dict[str, str]]]:
        blockers: list[dict[str, str]] = []
        metadata_path = self._session_metadata_path(session.emergency_session_id)
        if not os.path.isfile(metadata_path):
            blockers.append(_blocker(EmergencyMergeBlocker.LOCAL_INVALID, f"metadata missing: {metadata_path}"))
        status_allowed = {"active", "merge_pending", "merge_failed"}
        if session.status not in status_allowed:
            blockers.append(_blocker(EmergencyMergeBlocker.LOCAL_INVALID, f"status is {session.status}"))
        paths = self._local_paths(session)
        for label, path in paths.items():
            if not path or not os.path.isfile(path):
                blockers.append(_blocker(EmergencyMergeBlocker.LOCAL_INVALID, f"{label} missing: {path}"))
        validations = self._validate_local_files(session, paths, blockers)
        return not blockers, validations, blockers

    def validate_remote_side(self, session: EmergencySessionMetadata) -> tuple[bool, dict[str, Any], list[dict[str, str]], DbRuntimeContext | None]:
        blockers: list[dict[str, str]] = []
        context = self._network_context()
        path_error = _required_remote_paths_error(context)
        if path_error:
            blockers.append(_blocker(EmergencyMergeBlocker.REMOTE_INVALID, path_error))
            return False, {}, blockers, context
        medical = validate_medical_db_snapshot(context.medical_db_path)
        settings = validate_settings_db_snapshot(context.settings_db_path)
        if not medical.ok:
            blockers.append(_blocker(EmergencyMergeBlocker.REMOTE_INVALID, f"remote medical invalid: {medical.reason}"))
        if not settings.ok:
            blockers.append(_blocker(EmergencyMergeBlocker.REMOTE_INVALID, f"remote settings invalid: {settings.reason}"))
        policy_error = _client_policy_error(context.medical_client_policy_path)
        if policy_error:
            blockers.append(_blocker(EmergencyMergeBlocker.REMOTE_INVALID, policy_error))
        identity_error = _remote_identity_error(session, context.medical_db_path, medical)
        if identity_error:
            blockers.append(_blocker(EmergencyMergeBlocker.REMOTE_IDENTITY_MISMATCH, identity_error))
        return not blockers, _validation_payload(medical, settings), blockers, context

    def check_merge_locks(self, context: DbRuntimeContext | None = None) -> dict[str, Any]:
        context = context or self._network_context()
        db_lock = _probe_lock_file_available(context.medical_db_lock_path)
        merge_lock = emergency_merge_lock_path(context.baza_dir)
        merge_active = os.path.exists(merge_lock)
        return {
            "ok": not db_lock and not merge_active,
            "db_lock": "available" if not db_lock else db_lock,
            "emergency_merge_lock": "active" if merge_active else "available",
            "emergency_merge_lock_path": merge_lock,
        }

    def check_session_locks(self, context: DbRuntimeContext | None = None) -> dict[str, Any]:
        context = context or self._network_context()
        active = _existing_session_locks(context.session_locks_dir)
        return {
            "ok": not active,
            "session_locks_dir": context.session_locks_dir,
            "active_locks": active,
        }

    def classify_merge_mode(self, base_last: int, remote_last: int) -> tuple[str, str, list[dict[str, str]]]:
        if remote_last == base_last:
            return "ready_mode_a", EmergencyMergeMode.REMOTE_UNCHANGED_MODE_A.value, []
        if remote_last > base_last:
            return (
                "ready_emergency_authoritative",
                EmergencyMergeMode.REMOTE_CHANGED_EMERGENCY_AUTHORITATIVE.value,
                [],
            )
        return (
            "blocked_inconsistent",
            EmergencyMergeMode.REMOTE_INCONSISTENT.value,
            [_blocker(EmergencyMergeBlocker.INCONSISTENT, "remote_last_change_id is below base_last_change_id")],
        )

    def compute_change_summary(self, session: EmergencySessionMetadata) -> EmergencyChangeSummary:
        local_validation = validate_medical_db_snapshot(session.local_db_path)
        local_stat = os.stat(session.local_db_path)
        settings_hash = compute_file_hash(str(session.settings_snapshot_path)) if session.settings_snapshot_path else ""
        table_counts, admissions, patients, first_at, last_at = _change_log_summary(
            session.local_db_path,
            int(session.base_last_change_id or 0),
        )
        base_last = int(session.base_last_change_id or 0)
        local_last = int(local_validation.last_change_id or 0)
        return EmergencyChangeSummary(
            local_last_change_id=local_last,
            base_last_change_id=base_last,
            emergency_change_count=max(0, local_last - base_last),
            changed_tables_summary=table_counts,
            touched_admissions=admissions,
            touched_patients=patients,
            first_emergency_change_at=first_at,
            last_emergency_change_at=last_at,
            local_db_hash=compute_file_hash(session.local_db_path),
            local_db_size=int(local_stat.st_size),
            local_db_mtime=float(local_stat.st_mtime),
            base_snapshot_hash=compute_file_hash(session.base_snapshot_path),
            settings_snapshot_hash=settings_hash,
        )

    def write_dry_run_report(self, result: EmergencyMergeDryRunResult) -> str:
        report_path = self._dry_run_report_path(result.session_id)
        report = self._build_report(result)
        atomic_write_json(report_path, asdict(report))
        return report_path

    def get_latest_dry_run_report(self, session_id: str) -> dict[str, Any] | None:
        logs_dir = self._session_logs_dir(session_id)
        if not os.path.isdir(logs_dir):
            return None
        reports = sorted(
            path for path in os.listdir(logs_dir) if path.startswith("emergency_merge_dry_run_") and path.endswith(".json")
        )
        if not reports:
            return None
        return read_json_file(os.path.join(logs_dir, reports[-1]))

    def _run_dry_run(
        self,
        session_id: str,
        marker_path: str | None,
        session: EmergencySessionMetadata | None,
    ) -> EmergencyMergeDryRunResult:
        if session is None:
            return self._blocked(session_id, "blocked_local_invalid", EmergencyMergeBlocker.LOCAL_INVALID, "session metadata unavailable")
        role_block = self._role_or_runtime_block(session)
        if role_block is not None:
            return role_block
        marker_result = self._validate_marker(session, marker_path)
        if marker_result is not None:
            return marker_result
        local_ok, local_payload, local_blockers = self.validate_local_emergency_side(session)
        if not local_ok:
            return self._result(session, "blocked_local_invalid", EmergencyMergeMode.BLOCKED.value, local_blockers, local_payload)
        remote_ok, remote_payload, remote_blockers, context = self.validate_remote_side(session)
        if not remote_ok or context is None:
            return self._result(session, "blocked_remote_invalid", EmergencyMergeMode.BLOCKED.value, remote_blockers, local_payload, remote_payload)
        locks_status, lock_blockers = self._combined_lock_status(context)
        if lock_blockers:
            return self._result(
                session,
                "blocked_locks",
                EmergencyMergeMode.BLOCKED.value,
                lock_blockers,
                local_payload,
                remote_payload,
                locks_status=locks_status,
            )
        backup_status = self._check_backup_readiness(session, context)
        if not backup_status.get("ok"):
            return self._result(
                session,
                "blocked_backup_not_ready",
                EmergencyMergeMode.BLOCKED.value,
                [_blocker(EmergencyMergeBlocker.BACKUP_NOT_READY, str(backup_status.get("reason") or "backup not ready"))],
                local_payload,
                remote_payload,
                locks_status=locks_status,
                backup_status=backup_status,
            )
        result_status, mode, mode_blockers = self.classify_merge_mode(
            int(session.base_last_change_id or 0),
            int(remote_payload.get("medical", {}).get("last_change_id") or 0),
        )
        return self._result(
            session,
            result_status,
            mode,
            mode_blockers,
            local_payload,
            remote_payload,
            locks_status=locks_status,
            backup_status=backup_status,
        )

    def _validate_marker(self, session: EmergencySessionMetadata, marker_path: str | None) -> EmergencyMergeDryRunResult | None:
        path = marker_path or merge_ready_marker_path(self.store.resolve_root(), session.emergency_session_id)
        if not os.path.isfile(path):
            return self._result(
                session,
                "blocked_marker_required",
                EmergencyMergeMode.BLOCKED.value,
                [_blocker(EmergencyMergeBlocker.MARKER_REQUIRED, "merge-ready marker is missing")],
                marker_path=path,
            )
        try:
            marker = load_merge_ready_marker(path)
            ok, reason = validate_merge_ready_marker(marker, session, ttl_sec=self.marker_ttl_sec)
        except Exception as exc:
            ok, reason = False, str(exc)
        if not ok:
            return self._result(
                session,
                "blocked_marker_invalid",
                EmergencyMergeMode.BLOCKED.value,
                [_blocker(EmergencyMergeBlocker.MARKER_INVALID, reason)],
                marker_path=path,
            )
        return None

    def _role_or_runtime_block(self, session: EmergencySessionMetadata) -> EmergencyMergeDryRunResult | None:
        if self.role != "nurse":
            return self._result(
                session,
                "blocked_role_not_allowed",
                EmergencyMergeMode.BLOCKED.value,
                [_blocker(EmergencyMergeBlocker.ROLE_NOT_ALLOWED, "merge dry-run is allowed only for nurse")],
            )
        mode = str(getattr(self.runtime_context, "mode", "post_close_dry_run") or "")
        if mode not in {"emergency", "post_close_dry_run", "merge_dry_run"}:
            return self._result(
                session,
                "blocked_runtime_mode",
                EmergencyMergeMode.BLOCKED.value,
                [_blocker(EmergencyMergeBlocker.RUNTIME_MODE_NOT_ALLOWED, f"runtime mode is {mode}")],
            )
        return None

    def _validate_local_files(
        self,
        session: EmergencySessionMetadata,
        paths: dict[str, str | None],
        blockers: list[dict[str, str]],
    ) -> dict[str, Any]:
        local = validate_medical_db_snapshot(str(paths.get("local_db") or ""))
        base = validate_medical_db_snapshot(str(paths.get("base_snapshot") or ""))
        settings = validate_settings_db_snapshot(str(paths.get("settings_snapshot") or ""))
        for label, validation in (("local emergency DB", local), ("base snapshot", base), ("settings snapshot", settings)):
            if not validation.ok:
                blockers.append(_blocker(EmergencyMergeBlocker.LOCAL_INVALID, f"{label} invalid: {validation.reason}"))
        if base.ok and compute_file_hash(session.base_snapshot_path) != session.base_snapshot_hash:
            blockers.append(_blocker(EmergencyMergeBlocker.LOCAL_INVALID, "base_snapshot_hash mismatch"))
        if base.ok and int(base.last_change_id or 0) != int(session.base_last_change_id or 0):
            blockers.append(_blocker(EmergencyMergeBlocker.LOCAL_INVALID, "base_last_change_id mismatch"))
        if local.ok and base.ok and int(local.schema_version or 0) != int(base.schema_version or 0):
            blockers.append(_blocker(EmergencyMergeBlocker.LOCAL_INVALID, "local/base schema mismatch"))
        if local.ok and int(local.last_change_id or 0) < int(session.base_last_change_id or 0):
            blockers.append(_blocker(EmergencyMergeBlocker.LOCAL_INVALID, "local_last_change_id below base_last_change_id"))
        if _compare_client_versions(APP_VERSION, session.app_version) < 0:
            blockers.append(_blocker(EmergencyMergeBlocker.LOCAL_INVALID, "session app_version is newer than current client"))
        return {
            "local": _snapshot_payload(local),
            "base": _snapshot_payload(base),
            "settings": _snapshot_payload(settings),
        }

    def _combined_lock_status(self, context: DbRuntimeContext) -> tuple[dict[str, Any], list[dict[str, str]]]:
        merge = self.check_merge_locks(context)
        sessions = self.check_session_locks(context)
        blockers: list[dict[str, str]] = []
        if not merge.get("ok"):
            blockers.append(_blocker(EmergencyMergeBlocker.LOCKS, "remote db.lock or emergency_merge.lock is active"))
        if not sessions.get("ok"):
            blockers.append(_blocker(EmergencyMergeBlocker.LOCKS, "role/session lock is active"))
        return {"merge_locks": merge, "session_locks": sessions}, blockers

    def _check_backup_readiness(self, session: EmergencySessionMetadata, context: DbRuntimeContext) -> dict[str, Any]:
        checks = {
            "remote_backup_dir": _check_probe_writable_dir(context.medical_backups_valid_dir),
            "local_backup_dir": _check_probe_writable_dir(os.path.join(self._session_dir(session.emergency_session_id), "backups")),
            "remote_recovery_lock": "active" if os.path.exists(context.recovery_lock_path) else "available",
            "remote_rotation_lock": "active" if os.path.exists(context.medical_db_rotation_lock_path) else "available",
        }
        blocking = [f"{key}: {value}" for key, value in checks.items() if value != "ready" and value != "available"]
        return {"ok": not blocking, "checks": checks, "reason": "; ".join(blocking)}

    def _result(
        self,
        session: EmergencySessionMetadata,
        result_status: str,
        merge_mode: str,
        blockers: list[dict[str, str]],
        local_payload: dict[str, Any] | None = None,
        remote_payload: dict[str, Any] | None = None,
        *,
        locks_status: dict[str, Any] | None = None,
        backup_status: dict[str, Any] | None = None,
        marker_path: str | None = None,
    ) -> EmergencyMergeDryRunResult:
        summary = self._safe_change_summary(session)
        remote_medical = (remote_payload or {}).get("medical", {})
        local_medical = (local_payload or {}).get("local", {})
        return EmergencyMergeDryRunResult(
            ok=result_status in {"ready_mode_a", "ready_emergency_authoritative"},
            result_status=result_status,
            merge_mode=merge_mode,
            blockers=blockers,
            warnings=_warnings_for_result(result_status),
            session_id=session.emergency_session_id,
            marker_path=marker_path or merge_ready_marker_path(self.store.resolve_root(), session.emergency_session_id),
            user_message=_user_message(result_status),
            local_validation=local_payload or {},
            remote_validation=remote_payload or {},
            locks_status=locks_status or {},
            backup_readiness_status=backup_status or {},
            change_summary=summary,
            remote_fingerprint=dict(remote_medical.get("fingerprint") or {}),
            base_last_change_id=int(session.base_last_change_id or 0),
            local_last_change_id=int(local_medical.get("last_change_id") or summary.local_last_change_id or 0),
            remote_last_change_id=int(remote_medical.get("last_change_id") or 0),
        )

    def _blocked(
        self,
        session_id: str,
        result_status: str,
        blocker: EmergencyMergeBlocker,
        reason: str,
    ) -> EmergencyMergeDryRunResult:
        return EmergencyMergeDryRunResult(
            ok=False,
            result_status=result_status,
            blockers=[_blocker(blocker, reason)],
            session_id=session_id,
            user_message=_user_message(result_status),
        )

    def _build_report(self, result: EmergencyMergeDryRunResult) -> EmergencyMergeDryRunReport:
        summary = result.change_summary
        return EmergencyMergeDryRunReport(
            report_version=DRY_RUN_REPORT_VERSION,
            created_at=_now_text(),
            emergency_session_id=result.session_id,
            result_status=result.result_status,
            merge_mode=result.merge_mode,
            blockers=result.blockers,
            warnings=result.warnings,
            base_last_change_id=result.base_last_change_id,
            local_last_change_id=result.local_last_change_id,
            remote_last_change_id=result.remote_last_change_id,
            base_snapshot_hash=summary.base_snapshot_hash,
            local_db_hash=summary.local_db_hash,
            remote_fingerprint=result.remote_fingerprint,
            settings_snapshot_hash=summary.settings_snapshot_hash,
            changed_tables_summary=summary.changed_tables_summary,
            touched_admissions=summary.touched_admissions,
            touched_patients=summary.touched_patients,
            locks_status=result.locks_status,
            backup_readiness_status=result.backup_readiness_status,
            schema_versions=_schema_versions(result),
            app_version=APP_VERSION,
            next_allowed_action=_next_action(result.result_status),
        )

    def _update_session_dry_run_metadata(
        self,
        session: EmergencySessionMetadata,
        result: EmergencyMergeDryRunResult,
    ) -> None:
        error = "; ".join(str(item.get("reason") or "") for item in result.blockers) or None
        updated = replace(
            self.store.read_active_session(session.emergency_session_id),
            last_dry_run_at=_now_text(),
            last_dry_run_status=result.result_status,
            last_dry_run_report_path=result.report_path,
            last_dry_run_mode=result.merge_mode,
            last_dry_run_error=error,
        )
        self.store.write_active_session(updated)

    def _read_session_or_none(self, session_id: str) -> EmergencySessionMetadata | None:
        try:
            return self.store.read_active_session(session_id)
        except EmergencyMetadataError:
            return None

    def _network_context(self) -> DbRuntimeContext:
        context = self.network_context_factory()
        if not any((self.network_baza_dir, self.source_medical_db_path, self.source_settings_db_path)):
            return context
        return _context_with_overrides(
            context,
            baza_dir=self.network_baza_dir,
            medical_db_path=self.source_medical_db_path,
            settings_db_path=self.source_settings_db_path,
        )

    def _local_paths(self, session: EmergencySessionMetadata) -> dict[str, str | None]:
        return {
            "metadata": self._session_metadata_path(session.emergency_session_id),
            "local_db": session.local_db_path,
            "base_snapshot": session.base_snapshot_path,
            "settings_snapshot": session.settings_snapshot_path,
        }

    def _dry_run_report_path(self, session_id: str) -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        return os.path.join(self._session_logs_dir(session_id), f"emergency_merge_dry_run_{stamp}.json")

    def _session_dir(self, session_id: str) -> str:
        return active_session_dir(self.store.resolve_root(), session_id)

    def _session_logs_dir(self, session_id: str) -> str:
        return os.path.join(self._session_dir(session_id), "logs")

    def _session_metadata_path(self, session_id: str) -> str:
        return os.path.join(self._session_dir(session_id), "emergency_session.json")

    def _safe_change_summary(self, session: EmergencySessionMetadata) -> EmergencyChangeSummary:
        if not _local_summary_possible(session):
            return EmergencyChangeSummary()
        try:
            return self.compute_change_summary(session)
        except Exception:
            return EmergencyChangeSummary(base_last_change_id=int(session.base_last_change_id or 0))


def _infer_emergency_root(runtime_context: DbRuntimeContext | None) -> str | None:
    baza_dir = str(getattr(runtime_context, "baza_dir", "") or "")
    if not baza_dir:
        return None
    active_dir = os.path.dirname(os.path.abspath(os.path.normpath(baza_dir)))
    if os.path.basename(active_dir).lower() == "active":
        return os.path.dirname(active_dir)
    return None


def _optional_path(path: str | None) -> str | None:
    if not path:
        return None
    return os.path.abspath(os.path.normpath(str(path)))


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_marker_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _blocker(kind: EmergencyMergeBlocker, reason: str) -> dict[str, str]:
    return {"code": kind.value, "reason": str(reason or "")}


def _int_value(value: Any, *, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _snapshot_payload(validation: SnapshotValidationResult) -> dict[str, Any]:
    return {
        "ok": bool(validation.ok),
        "reason": validation.reason,
        "schema_version": int(validation.schema_version or 0),
        "last_change_id": int(validation.last_change_id or 0),
        "file_hash": validation.file_hash,
        "file_size": int(validation.file_size or 0),
        "file_mtime": float(validation.file_mtime or 0.0),
        "fingerprint": dict(validation.fingerprint or {}),
    }


def _validation_payload(medical: SnapshotValidationResult, settings: SnapshotValidationResult) -> dict[str, Any]:
    return {"medical": _snapshot_payload(medical), "settings": _snapshot_payload(settings)}


def _required_remote_paths_error(context: DbRuntimeContext) -> str:
    checks = (
        ("network BAZA_DIR", context.baza_dir, os.path.isdir),
        ("remote medical DB", context.medical_db_path, os.path.isfile),
        ("remote settings DB", context.settings_db_path, os.path.isfile),
    )
    for label, path, predicate in checks:
        if not predicate(path):
            return f"{label} unavailable: {path}"
    return ""


def _client_policy_error(policy_path: str) -> str:
    try:
        with open(policy_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception as exc:
        return f"client policy unavailable: {exc}"
    if not isinstance(payload, dict):
        return "client policy is not a JSON object"
    min_version = str(payload.get("min_client_version") or APP_VERSION)
    if _compare_client_versions(APP_VERSION, min_version) < 0:
        return "client policy min_client_version blocks this client"
    if str(payload.get("required_db_profile") or "").strip() != NETWORK_SAFE_DB_PROFILE:
        return "client policy required_db_profile mismatch"
    if bool(payload.get("wal_allowed_on_shared_db")):
        return "client policy allows WAL on shared DB"
    return ""


def _remote_identity_error(
    session: EmergencySessionMetadata,
    remote_path: str,
    validation: SnapshotValidationResult,
) -> str:
    if not validation.ok:
        return ""
    return validate_remote_identity_error(session, remote_path, validation)


def _path_identity_compatible(base_path: str, remote_path: str) -> bool:
    return remote_identity_paths_match(base_path, remote_path)


def _probe_lock_file_available(lock_path: str) -> str:
    directory = os.path.dirname(lock_path)
    if not os.path.isdir(directory):
        return f"lock directory unavailable: {directory}"
    payload = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}:dry_run"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        os.remove(lock_path)
        return ""
    except FileExistsError:
        return f"active lock: {lock_path}"
    except OSError as exc:
        return str(exc)


def _existing_session_locks(directory: str) -> list[str]:
    if not os.path.isdir(directory):
        return []
    locks = []
    for name in (
        "doctor.lock",
        "nurse.lock",
        "operblock.lock",
        "operblock_emergency.lock",
        "operblock_planned.lock",
    ):
        path = os.path.join(directory, name)
        if os.path.exists(path):
            locks.append(path)
    return locks


def _check_probe_writable_dir(directory: str) -> str:
    try:
        if os.path.exists(directory) and not os.path.isdir(directory):
            return f"not a directory: {directory}"
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f".dry_run_probe_{os.getpid()}_{uuid.uuid4().hex}.tmp")
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, b"dry-run")
        finally:
            os.close(fd)
        os.remove(path)
        return "ready"
    except OSError as exc:
        return str(exc)


def _context_with_overrides(
    context: DbRuntimeContext,
    *,
    baza_dir: str | None,
    medical_db_path: str | None,
    settings_db_path: str | None,
) -> DbRuntimeContext:
    effective_baza = _optional_path(baza_dir) or context.baza_dir
    effective_medical = _optional_path(medical_db_path) or context.medical_db_path
    effective_settings = _optional_path(settings_db_path) or context.settings_db_path
    return replace(
        context,
        baza_dir=effective_baza,
        medical_db_path=effective_medical,
        medical_db_lock_path=os.path.join(os.path.dirname(effective_medical), "db.lock"),
        medical_backups_valid_dir=os.path.join(effective_baza, "backups", "valid"),
        medical_backup_health_dir=os.path.join(effective_baza, "backup_health"),
        recovery_lock_path=os.path.join(effective_baza, "locks", "recovery.lock"),
        session_locks_dir=os.path.join(effective_baza, "session_locks"),
        settings_db_path=effective_settings,
        settings_db_lock_path=os.path.join(os.path.dirname(effective_settings), "settings.db.lock"),
        settings_backup_health_dir=os.path.join(os.path.dirname(effective_settings), "backup_health"),
        medical_db_rotation_lock_path=os.path.join(os.path.dirname(effective_medical), "db_rotation.lock"),
        medical_client_policy_path=os.path.join(effective_baza, "config", "client_policy.json"),
    )


def _change_log_summary(db_path: str, base_last_change_id: int) -> tuple[dict[str, dict[str, int]], list[int], list[int], str | None, str | None]:
    conn = _open_readonly(db_path)
    try:
        columns = _table_columns(conn, "change_log")
        if not columns:
            return {}, [], [], None, None
        rows = conn.execute("SELECT * FROM change_log WHERE id > ? ORDER BY id", (int(base_last_change_id or 0),)).fetchall()
    finally:
        conn.close()
    return _summarize_change_rows(rows)


def _open_readonly(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True, isolation_level=None, timeout=5.0)
    configure_connection(conn, readonly=True, profile="network")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.DatabaseError:
        return set()


def _summarize_change_rows(rows: list[sqlite3.Row]) -> tuple[dict[str, dict[str, int]], list[int], list[int], str | None, str | None]:
    table_counts: dict[str, dict[str, int]] = {}
    admissions: set[int] = set()
    patients: set[int] = set()
    timestamps: list[str] = []
    for row in rows:
        payload = dict(row)
        table = str(payload.get("table_name") or payload.get("entity_name") or payload.get("entity") or "unknown")
        action = str(payload.get("action") or payload.get("operation") or "unknown")
        table_counts.setdefault(table, {})
        table_counts[table][action] = int(table_counts[table].get(action, 0)) + 1
        _collect_touched_ids(payload, table, admissions, patients)
        ts = payload.get("changed_at") or payload.get("created_at") or payload.get("timestamp")
        if ts:
            timestamps.append(str(ts))
    return table_counts, sorted(admissions), sorted(patients), (min(timestamps) if timestamps else None), (max(timestamps) if timestamps else None)


def _collect_touched_ids(payload: dict[str, Any], table: str, admissions: set[int], patients: set[int]) -> None:
    for key, target in (("admission_id", admissions), ("patient_id", patients)):
        value = payload.get(key)
        if value is not None:
            try:
                target.add(int(value))
            except (TypeError, ValueError):
                pass
    entity_id = payload.get("entity_id")
    try:
        numeric_id = int(entity_id)
    except (TypeError, ValueError):
        return
    if "admission" in table:
        admissions.add(numeric_id)
    if "patient" in table:
        patients.add(numeric_id)


def _local_summary_possible(session: EmergencySessionMetadata) -> bool:
    return bool(
        session.local_db_path
        and session.base_snapshot_path
        and session.settings_snapshot_path
        and os.path.isfile(session.local_db_path)
        and os.path.isfile(session.base_snapshot_path)
        and os.path.isfile(str(session.settings_snapshot_path))
    )


def _warnings_for_result(result_status: str) -> list[str]:
    warnings = ["change_log is index only, not full replay log"]
    if result_status == "ready_emergency_authoritative":
        warnings.append("remote medical DB changed after emergency base; row-level merge will preserve remote-only rows")
        warnings.append("local emergency rows win RemCard conflicts")
        warnings.append("remote settings DB will not be replaced")
    return warnings


def _user_message(result_status: str) -> str:
    if result_status == "ready_mode_a":
        return READY_MODE_A_MESSAGE
    if result_status == "ready_emergency_authoritative":
        return REMOTE_CHANGED_AUTHORITATIVE_MESSAGE
    return "Проверка объединения заблокирована. Аварийная база сохранена."


def _next_action(result_status: str) -> str:
    mapping = {
        "ready_mode_a": "run_real_merge_row_level",
        "ready_emergency_authoritative": "run_real_merge_row_level_emergency_authoritative",
        "blocked_marker_required": "continue_emergency",
        "blocked_marker_invalid": "manual_support_required",
        "blocked_inconsistent": "manual_support_required",
    }
    return mapping.get(result_status, "manual_support_required")


def _schema_versions(result: EmergencyMergeDryRunResult) -> dict[str, int]:
    local = result.local_validation or {}
    remote = result.remote_validation or {}
    return {
        "local_medical": int((local.get("local") or {}).get("schema_version") or 0),
        "base_snapshot": int((local.get("base") or {}).get("schema_version") or 0),
        "settings_snapshot": int((local.get("settings") or {}).get("schema_version") or 0),
        "remote_medical": int((remote.get("medical") or {}).get("schema_version") or 0),
        "remote_settings": int((remote.get("settings") or {}).get("schema_version") or 0),
    }
