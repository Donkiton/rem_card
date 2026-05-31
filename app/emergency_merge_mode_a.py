from __future__ import annotations

import os
import shutil
import socket
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable

from rem_card.app.db_runtime_context import DbRuntimeContext, build_network_runtime_context
from rem_card.app.emergency_merge_dry_run import (
    DEFAULT_MARKER_TTL_SEC,
    EmergencyMergeDryRunService,
    load_merge_ready_marker,
    validate_merge_ready_marker,
)
from rem_card.app.emergency_metadata import (
    EmergencyMetadataError,
    EmergencySessionMetadata,
    atomic_write_json,
    read_json_file,
)
from rem_card.app.emergency_paths import active_session_dir, archived_session_dir
from rem_card.app.emergency_remote_identity import validate_remote_identity_error
from rem_card.app.emergency_restore_probe import (
    clear_merge_ready_marker,
    emergency_merge_lock_path,
    merge_ready_marker_path,
)
from rem_card.app.emergency_standby import EmergencyStandbyManager
from rem_card.app.emergency_store import EmergencyLocalStore
from rem_card.app.emergency_validation import (
    SnapshotValidationResult,
    compute_file_hash,
    validate_medical_db_snapshot,
    validate_settings_db_snapshot,
)
from rem_card.app.runtime_outage import runtime_outage_startup_request_path
from rem_card.app.sqlite_shared import FileWriteLock, backup_connection, configure_connection, run_integrity_check
from rem_card.app.startup_db_guard import _compare_client_versions
from rem_card.app.version import APP_VERSION


MODE_A_MERGE_REPORT_VERSION = 1
MODE_A_MERGE_MODE = "emergency_authoritative_replacement"
DEFAULT_DRY_RUN_REPORT_TTL_SEC = max(
    60,
    int(float(os.environ.get("REMCARD_MERGE_DRY_RUN_REPORT_TTL_SEC", "86400"))),
)

MERGE_SUCCESS_MESSAGE = (
    "Объединение завершено.\n"
    "Работа снова будет продолжена с сетевой базой."
)
MERGE_FAILURE_MESSAGE = (
    "Объединение не завершено.\n"
    "Аварийная база сохранена.\n"
    "Работа остаётся в аварийном режиме."
)
REMOTE_CHANGED_AUTHORITATIVE_MESSAGE = (
    "Сетевая база изменилась после создания аварийной копии.\n"
    "Будет выполнено emergency-authoritative объединение: локальная аварийная медицинская БД "
    "заменит сетевую медицинскую БД после резервных копий и проверок.\n"
    "Сетевая БД настроек заменяться не будет."
)


class EmergencyModeAMergeStatus(str, Enum):
    SUCCESS = "success"
    BLOCKED = "blocked"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class EmergencyModeAMergeError(RuntimeError):
    pass


@dataclass(frozen=True)
class EmergencyModeAMergeReport:
    report_version: int
    created_at: str
    emergency_session_id: str
    result_status: str
    mode: str
    base_last_change_id: int
    local_last_change_id: int
    remote_last_change_id_before: int
    remote_last_change_id_after: int
    remote_backup_path: str
    local_backup_path: str
    temp_db_path: str
    temp_db_hash: str
    final_remote_hash: str
    post_quick_check_status: str
    post_integrity_check_status: str | None
    applied_change_count_estimate: int
    changed_tables_summary: dict[str, Any]
    locks_acquired: dict[str, Any]
    session_locks_status: dict[str, Any]
    started_at: str
    finished_at: str
    duration_ms: int
    error: str | None
    rollback_status: str | None
    blockers: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dry_run_report_path: str = ""
    marker_path: str = ""
    pre_merge_remote_path: str = ""
    archive_path: str = ""
    fresh_standby_status: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EmergencyModeAMergeResult:
    ok: bool
    result_status: str
    error_code: str = ""
    error: str | None = None
    session_id: str = ""
    report_path: str = ""
    user_message: str = MERGE_FAILURE_MESSAGE
    blockers: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    marker_path: str = ""
    dry_run_report_path: str = ""
    remote_backup_path: str = ""
    local_backup_path: str = ""
    temp_db_path: str = ""
    temp_db_hash: str = ""
    pre_merge_remote_path: str = ""
    final_remote_hash: str = ""
    post_quick_check_status: str = ""
    post_integrity_check_status: str | None = None
    base_last_change_id: int = 0
    local_last_change_id: int = 0
    remote_last_change_id_before: int = 0
    remote_last_change_id_after: int = 0
    applied_change_count_estimate: int = 0
    changed_tables_summary: dict[str, Any] = field(default_factory=dict)
    locks_acquired: dict[str, Any] = field(default_factory=dict)
    session_locks_status: dict[str, Any] = field(default_factory=dict)
    rollback_status: str | None = None
    archive_path: str = ""
    fresh_standby_status: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _Prerequisites:
    ok: bool
    session: EmergencySessionMetadata | None = None
    context: DbRuntimeContext | None = None
    marker_path: str = ""
    dry_run_report_path: str = ""
    marker: dict[str, Any] = field(default_factory=dict)
    dry_run_report: dict[str, Any] = field(default_factory=dict)
    local_validation: dict[str, Any] = field(default_factory=dict)
    remote_validation: dict[str, Any] = field(default_factory=dict)
    blockers: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class EmergencyModeAMergeService:
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
        dry_run_report_ttl_sec: int = DEFAULT_DRY_RUN_REPORT_TTL_SEC,
        before_replace_hook: Callable[[], None] | None = None,
        after_temp_created_hook: Callable[[str], None] | None = None,
    ):
        self.role = str(role or "").strip().lower()
        self.runtime_context = runtime_context
        self.store = store or EmergencyLocalStore(root=_infer_emergency_root(runtime_context))
        self.network_context_factory = network_context_factory or build_network_runtime_context
        self.source_medical_db_path = _optional_path(source_medical_db_path)
        self.source_settings_db_path = _optional_path(source_settings_db_path)
        self.network_baza_dir = _optional_path(network_baza_dir)
        self.marker_ttl_sec = max(1, int(marker_ttl_sec or 1))
        self.dry_run_report_ttl_sec = max(1, int(dry_run_report_ttl_sec or 1))
        self.before_replace_hook = before_replace_hook
        self.after_temp_created_hook = after_temp_created_hook
        self._dry_run_service = EmergencyMergeDryRunService(
            role=role,
            runtime_context=runtime_context,
            store=self.store,
            network_context_factory=self.network_context_factory,
            source_medical_db_path=self.source_medical_db_path,
            source_settings_db_path=self.source_settings_db_path,
            network_baza_dir=self.network_baza_dir,
            marker_ttl_sec=self.marker_ttl_sec,
        )

    def run_merge(
        self,
        session_id: str,
        dry_run_report_path: str | None = None,
        marker_path: str | None = None,
    ) -> EmergencyModeAMergeResult:
        started_at = _now_text()
        started = time.perf_counter()
        report_path = self._merge_report_path(session_id)
        locks: list[FileWriteLock] = []
        session: EmergencySessionMetadata | None = None
        result: EmergencyModeAMergeResult
        try:
            prereq = self.load_and_validate_prerequisites(session_id, dry_run_report_path, marker_path)
            session = prereq.session
            if not prereq.ok or session is None or prereq.context is None:
                result = self._result(
                    session_id=session_id,
                    status=EmergencyModeAMergeStatus.BLOCKED,
                    error_code=_first_blocker_code(prereq.blockers, "prerequisites_blocked"),
                    error=_first_blocker_reason(prereq.blockers),
                    blockers=prereq.blockers,
                    warnings=prereq.warnings,
                    report_path=report_path,
                    marker_path=prereq.marker_path,
                    dry_run_report_path=prereq.dry_run_report_path,
                    started_at=started_at,
                    started=started,
                )
                self._mark_merge_failed_if_possible(session, result)
                self.write_merge_report(result)
                return result

            lock_status = self.acquire_merge_locks(prereq.context)
            if not lock_status.get("ok"):
                result = self._blocked_result_from_status(
                    session,
                    prereq,
                    "locks_unavailable",
                    str(lock_status.get("reason") or "merge locks unavailable"),
                    report_path,
                    started_at,
                    started,
                    locks_acquired=lock_status,
                )
                self._mark_merge_failed_if_possible(session, result)
                self.write_merge_report(result)
                return result
            locks = list(lock_status.get("_locks") or [])

            session_locks = self._recheck_session_locks(prereq.context)
            if not session_locks.get("ok"):
                result = self._blocked_result_from_status(
                    session,
                    prereq,
                    "active_session_lock",
                    "role/session lock is active",
                    report_path,
                    started_at,
                    started,
                    locks_acquired=_public_lock_status(lock_status),
                    session_locks_status=session_locks,
                )
                self._mark_merge_failed_if_possible(session, result)
                self.write_merge_report(result)
                return result

            remote_before = self._validate_remote_unchanged(session, prereq.context)
            if not remote_before.get("ok"):
                result = self._blocked_result_from_status(
                    session,
                    prereq,
                    str(remote_before.get("code") or "remote_not_mode_a"),
                    str(remote_before.get("reason") or "remote is not Mode A"),
                    report_path,
                    started_at,
                    started,
                    locks_acquired=_public_lock_status(lock_status),
                    session_locks_status=session_locks,
                    remote_last_change_id_before=int(remote_before.get("remote_last_change_id") or 0),
                )
                self._mark_merge_failed_if_possible(session, result)
                self.write_merge_report(result)
                return result

            backups = self.create_pre_merge_backups(session, prereq.context)
            temp_info = self.create_validated_temp_remote_db(
                session,
                prereq.context,
                expected_last_change_id=int(remote_before["local_last_change_id"]),
            )
            if self.before_replace_hook is not None:
                self.before_replace_hook()
            remote_before_replace = self._validate_remote_unchanged(session, prereq.context)
            if not remote_before_replace.get("ok"):
                result = self._blocked_result_from_status(
                    session,
                    prereq,
                    str(remote_before_replace.get("code") or "remote_not_mode_a"),
                    str(remote_before_replace.get("reason") or "remote changed before replacement"),
                    report_path,
                    started_at,
                    started,
                    locks_acquired=_public_lock_status(lock_status),
                    session_locks_status=session_locks,
                    remote_last_change_id_before=int(remote_before_replace.get("remote_last_change_id") or 0),
                )
                result = replace(
                    result,
                    remote_backup_path=backups["remote_backup_path"],
                    local_backup_path=backups["local_backup_path"],
                    temp_db_path=temp_info["temp_db_path"],
                    temp_db_hash=temp_info["temp_db_hash"],
                )
                self._mark_merge_failed_if_possible(session, result)
                self.write_merge_report(result)
                return result
            replacement = self.replace_remote_db_safely(
                session,
                prereq.context,
                temp_info["temp_db_path"],
            )
            final_validation = self.validate_final_remote_db(
                prereq.context,
                expected_last_change_id=int(remote_before["local_last_change_id"]),
            )
            if not final_validation.get("ok"):
                rollback = self.rollback_remote_if_needed(
                    prereq.context,
                    replacement.get("pre_merge_remote_path", ""),
                    reason=str(final_validation.get("reason") or "final validation failed"),
                )
                result = self._result(
                    session_id=session.emergency_session_id,
                    status=EmergencyModeAMergeStatus.ROLLED_BACK,
                    error_code="final_validation_failed",
                    error=str(final_validation.get("reason") or "final validation failed"),
                    blockers=[_blocker("final_validation_failed", str(final_validation.get("reason") or ""))],
                    warnings=prereq.warnings,
                    report_path=report_path,
                    marker_path=prereq.marker_path,
                    dry_run_report_path=prereq.dry_run_report_path,
                    remote_backup_path=backups["remote_backup_path"],
                    local_backup_path=backups["local_backup_path"],
                    temp_db_path=temp_info["temp_db_path"],
                    temp_db_hash=temp_info["temp_db_hash"],
                    pre_merge_remote_path=replacement.get("pre_merge_remote_path", ""),
                    base_last_change_id=int(session.base_last_change_id or 0),
                    local_last_change_id=int(remote_before["local_last_change_id"]),
                    remote_last_change_id_before=int(remote_before["remote_last_change_id"]),
                    changed_tables_summary=_changed_tables_summary(prereq.dry_run_report),
                    applied_change_count_estimate=_applied_change_count(prereq.dry_run_report),
                    locks_acquired=_public_lock_status(lock_status),
                    session_locks_status=session_locks,
                    rollback_status=rollback.get("status"),
                    started_at=started_at,
                    started=started,
                )
                self._mark_merge_failed_if_possible(session, result)
                self.write_merge_report(result)
                return result

            remote_after = validate_medical_db_snapshot(prereq.context.medical_db_path)
            fresh_standby = self.create_fresh_standby_after_merge(prereq.context)
            result = self._build_success_result(
                session=session,
                prereq=prereq,
                report_path=report_path,
                backups=backups,
                temp_info=temp_info,
                replacement=replacement,
                final_validation=final_validation,
                remote_before=remote_before,
                remote_after=remote_after,
                locks_acquired=_public_lock_status(lock_status),
                session_locks=session_locks,
                fresh_standby=fresh_standby,
                started_at=started_at,
                started=started,
            )
            self.write_merge_report(result)
            self.mark_session_merged(session, result)
            self._clear_success_markers(session)
            archived_path = self.archive_emergency_session(session.emergency_session_id)
            active_dir_path = active_session_dir(self.store.resolve_root(), session.emergency_session_id)
            final_result = replace(
                result,
                report_path=_map_session_path(report_path, active_dir_path, archived_path),
                dry_run_report_path=_map_session_path(result.dry_run_report_path, active_dir_path, archived_path),
                local_backup_path=_map_session_path(result.local_backup_path, active_dir_path, archived_path),
                archive_path=archived_path,
            )
            self.write_merge_report(final_result)
            return final_result
        except Exception as exc:
            result = self._result(
                session_id=session_id,
                status=EmergencyModeAMergeStatus.FAILED,
                error_code="merge_failed",
                error=str(exc),
                blockers=[_blocker("merge_failed", str(exc))],
                report_path=report_path,
                marker_path=marker_path or "",
                dry_run_report_path=dry_run_report_path or "",
                started_at=started_at,
                started=started,
            )
            self._mark_merge_failed_if_possible(session, result)
            self.write_merge_report(result)
            return result
        finally:
            for lock in reversed(locks):
                lock.release()

    def load_and_validate_prerequisites(
        self,
        session_id: str,
        dry_run_report_path: str | None = None,
        marker_path: str | None = None,
    ) -> _Prerequisites:
        blockers: list[dict[str, str]] = []
        warnings: list[str] = []
        try:
            session = self.store.read_active_session(session_id)
        except EmergencyMetadataError as exc:
            return _Prerequisites(ok=False, blockers=[_blocker("local_session_invalid", str(exc))])
        if self.role != "nurse" or str(session.source_role or "").lower() != "nurse":
            blockers.append(_blocker("role_not_allowed", "Mode A merge is allowed only for nurse emergency sessions"))
        mode = str(getattr(self.runtime_context, "mode", "post_close_merge") or "")
        if mode not in {"emergency", "post_close_merge", "merge_mode_a", "merge"}:
            blockers.append(_blocker("runtime_mode_not_allowed", f"runtime mode is {mode}"))
        if session.status not in {"active", "merge_pending", "merge_failed"}:
            blockers.append(_blocker("session_status_invalid", f"session status is {session.status}"))
        if _compare_client_versions(APP_VERSION, str(session.app_version or APP_VERSION)) < 0:
            blockers.append(_blocker("app_version_incompatible", "session app_version is newer than current client"))

        marker_file = marker_path or merge_ready_marker_path(self.store.resolve_root(), session.emergency_session_id)
        marker = self._load_marker(session, marker_file, blockers)
        dry_report_file = dry_run_report_path or self._latest_dry_run_report_path(session.emergency_session_id)
        dry_report = self._load_dry_run_report(session, dry_report_file, blockers)
        warnings.extend(str(item) for item in (dry_report.get("warnings") or []) if item)

        local_ok, local_payload, local_blockers = self._dry_run_service.validate_local_emergency_side(session)
        if not local_ok:
            blockers.extend(_prefix_blockers(local_blockers, "local_invalid"))
        context = self._network_context()
        remote_ok, remote_payload, remote_blockers, remote_context = self._dry_run_service.validate_remote_side(session)
        context = remote_context or context
        if not remote_ok:
            blockers.extend(_prefix_blockers(remote_blockers, "remote_invalid"))

        warnings.extend(self._settings_warnings(session, remote_payload))
        if blockers:
            return _Prerequisites(
                ok=False,
                session=session,
                context=context,
                marker_path=marker_file,
                dry_run_report_path=dry_report_file or "",
                marker=marker,
                dry_run_report=dry_report,
                local_validation=local_payload,
                remote_validation=remote_payload,
                blockers=blockers,
                warnings=warnings,
            )
        return _Prerequisites(
            ok=True,
            session=session,
            context=context,
            marker_path=marker_file,
            dry_run_report_path=dry_report_file or "",
            marker=marker,
            dry_run_report=dry_report,
            local_validation=local_payload,
            remote_validation=remote_payload,
            warnings=warnings,
        )

    def acquire_merge_locks(self, context: DbRuntimeContext) -> dict[str, Any]:
        owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}:mode_a_merge"
        db_lock = FileWriteLock(context.medical_db_lock_path, stale_timeout_sec=10 * 60)
        if not db_lock.acquire(owner, "emergency_mode_a_merge"):
            return {"ok": False, "reason": f"remote db.lock is active: {context.medical_db_lock_path}", "_locks": []}
        merge_lock = FileWriteLock(emergency_merge_lock_path(context.baza_dir), stale_timeout_sec=10 * 60)
        if not merge_lock.acquire(owner, "emergency_mode_a_merge"):
            db_lock.release()
            return {"ok": False, "reason": f"emergency_merge.lock is active: {merge_lock.lock_path}", "_locks": []}
        return {
            "ok": True,
            "db_lock": context.medical_db_lock_path,
            "emergency_merge_lock": merge_lock.lock_path,
            "_locks": [db_lock, merge_lock],
        }

    def create_pre_merge_backups(self, session: EmergencySessionMetadata, context: DbRuntimeContext) -> dict[str, str]:
        short_token = uuid.uuid4().hex[:8]
        remote_backup_path = os.path.join(
            os.path.join(context.baza_dir, "merge_bak"),
            f"r_{short_token}.db",
        )
        local_backup_path = os.path.join(
            active_session_dir(self.store.resolve_root(), session.emergency_session_id),
            "local_backups",
            f"l_{short_token}.db",
        )
        self._sqlite_backup(
            context.medical_db_path,
            remote_backup_path,
            invalid_dir=context.medical_invalid_backups_dir,
            source="emergency_mode_a_remote_pre_merge_backup",
        )
        remote_validation = validate_medical_db_snapshot(remote_backup_path)
        if not remote_validation.ok:
            raise EmergencyModeAMergeError(f"remote pre-merge backup validation failed: {remote_validation.reason}")
        self._sqlite_backup(
            session.local_db_path,
            local_backup_path,
            invalid_dir=os.path.join(active_session_dir(self.store.resolve_root(), session.emergency_session_id), "backup_health", "invalid_backups"),
            source="emergency_mode_a_local_emergency_backup",
        )
        local_validation = validate_medical_db_snapshot(local_backup_path)
        if not local_validation.ok:
            raise EmergencyModeAMergeError(f"local emergency backup validation failed: {local_validation.reason}")
        return {"remote_backup_path": remote_backup_path, "local_backup_path": local_backup_path}

    def create_validated_temp_remote_db(
        self,
        session: EmergencySessionMetadata,
        context: DbRuntimeContext,
        *,
        expected_last_change_id: int,
    ) -> dict[str, Any]:
        temp_path = os.path.join(
            os.path.join(context.baza_dir, "merge_tmp"),
            f"t_{uuid.uuid4().hex[:8]}.db",
        )
        self._quarantine_existing_temp(temp_path)
        self._sqlite_backup(
            session.local_db_path,
            temp_path,
            invalid_dir=context.medical_invalid_backups_dir,
            source="emergency_mode_a_temp_from_local_backup_api",
            validate=False,
        )
        if self.after_temp_created_hook is not None:
            self.after_temp_created_hook(temp_path)
        temp_validation = validate_medical_db_snapshot(temp_path)
        if not temp_validation.ok:
            self._quarantine_temp(temp_path, "invalid")
            raise EmergencyModeAMergeError(f"temp DB validation failed: {temp_validation.reason}")
        if int(temp_validation.last_change_id or 0) != int(expected_last_change_id or 0):
            self._quarantine_temp(temp_path, "last_change_mismatch")
            raise EmergencyModeAMergeError("temp DB last_change_id mismatch")
        return {
            "temp_db_path": temp_path,
            "temp_db_hash": temp_validation.file_hash,
            "temp_db_size": temp_validation.file_size,
            "temp_last_change_id": int(temp_validation.last_change_id or 0),
        }

    def replace_remote_db_safely(
        self,
        session: EmergencySessionMetadata,
        context: DbRuntimeContext,
        temp_db_path: str,
    ) -> dict[str, str]:
        recheck = self._validate_remote_unchanged(session, context)
        if not recheck.get("ok"):
            raise EmergencyModeAMergeError(str(recheck.get("reason") or "remote changed before replacement"))
        pre_merge_path = _unique_pre_merge_path(context.medical_db_path, session.emergency_session_id)
        remote_moved = False
        try:
            _replace_file_with_retry(context.medical_db_path, pre_merge_path)
            remote_moved = True
            _replace_file_with_retry(temp_db_path, context.medical_db_path)
            return {"pre_merge_remote_path": pre_merge_path}
        except Exception:
            if remote_moved and os.path.exists(pre_merge_path):
                try:
                    if os.path.exists(context.medical_db_path):
                        self._quarantine_temp(context.medical_db_path, "failed_replace_remote")
                    _replace_file_with_retry(pre_merge_path, context.medical_db_path)
                except OSError:
                    pass
            raise

    def validate_final_remote_db(
        self,
        context: DbRuntimeContext,
        *,
        expected_last_change_id: int,
    ) -> dict[str, Any]:
        validation = validate_medical_db_snapshot(context.medical_db_path)
        if not validation.ok:
            return {"ok": False, "reason": validation.reason, "quick_check": validation.reason}
        if int(validation.last_change_id or 0) != int(expected_last_change_id or 0):
            return {
                "ok": False,
                "reason": "final remote last_change_id mismatch",
                "quick_check": validation.reason,
                "last_change_id": int(validation.last_change_id or 0),
            }
        integrity_ok, integrity_reason = _run_integrity_check_path(context.medical_db_path)
        if not integrity_ok:
            return {
                "ok": False,
                "reason": f"final remote integrity_check failed: {integrity_reason}",
                "quick_check": validation.reason,
                "integrity_check": integrity_reason,
                "last_change_id": int(validation.last_change_id or 0),
            }
        return {
            "ok": True,
            "reason": "ok",
            "quick_check": validation.reason,
            "integrity_check": integrity_reason,
            "last_change_id": int(validation.last_change_id or 0),
            "file_hash": validation.file_hash,
        }

    def rollback_remote_if_needed(self, context: DbRuntimeContext, pre_merge_remote_path: str, *, reason: str) -> dict[str, Any]:
        if not pre_merge_remote_path or not os.path.exists(pre_merge_remote_path):
            return {"status": "rollback_not_available", "reason": reason}
        try:
            if os.path.exists(context.medical_db_path):
                self._quarantine_temp(context.medical_db_path, "failed_final_remote")
            _replace_file_with_retry(pre_merge_remote_path, context.medical_db_path)
            validation = validate_medical_db_snapshot(context.medical_db_path)
            if validation.ok:
                return {"status": "restored", "reason": reason, "remote_last_change_id": int(validation.last_change_id or 0)}
            return {"status": "restore_validation_failed", "reason": validation.reason}
        except Exception as exc:
            return {"status": "rollback_failed", "reason": str(exc)}

    def write_merge_report(self, result: EmergencyModeAMergeResult) -> str:
        report = EmergencyModeAMergeReport(
            report_version=MODE_A_MERGE_REPORT_VERSION,
            created_at=_now_text(),
            emergency_session_id=result.session_id,
            result_status=result.result_status,
            mode=MODE_A_MERGE_MODE,
            base_last_change_id=result.base_last_change_id,
            local_last_change_id=result.local_last_change_id,
            remote_last_change_id_before=result.remote_last_change_id_before,
            remote_last_change_id_after=result.remote_last_change_id_after,
            remote_backup_path=result.remote_backup_path,
            local_backup_path=result.local_backup_path,
            temp_db_path=result.temp_db_path,
            temp_db_hash=result.temp_db_hash,
            final_remote_hash=result.final_remote_hash,
            post_quick_check_status=result.post_quick_check_status,
            post_integrity_check_status=result.post_integrity_check_status,
            applied_change_count_estimate=result.applied_change_count_estimate,
            changed_tables_summary=result.changed_tables_summary,
            locks_acquired=result.locks_acquired,
            session_locks_status=result.session_locks_status,
            started_at=result.started_at,
            finished_at=result.finished_at,
            duration_ms=result.duration_ms,
            error=result.error,
            rollback_status=result.rollback_status,
            blockers=result.blockers,
            warnings=result.warnings,
            dry_run_report_path=result.dry_run_report_path,
            marker_path=result.marker_path,
            pre_merge_remote_path=result.pre_merge_remote_path,
            archive_path=result.archive_path,
            fresh_standby_status=result.fresh_standby_status,
        )
        atomic_write_json(result.report_path, asdict(report))
        return result.report_path

    def mark_session_merged(self, session: EmergencySessionMetadata, result: EmergencyModeAMergeResult) -> EmergencySessionMetadata:
        active_dir_path = active_session_dir(self.store.resolve_root(), session.emergency_session_id)
        archive_dir_path = archived_session_dir(self.store.resolve_root(), session.emergency_session_id)
        archived_report_path = _map_session_path(result.report_path, active_dir_path, archive_dir_path)
        archived_local_backup_path = _map_session_path(result.local_backup_path, active_dir_path, archive_dir_path)
        updated = replace(
            self.store.read_active_session(session.emergency_session_id),
            status="merged",
            ended_at=_now_text(),
            merged_at=_now_text(),
            local_db_path=_map_session_path(session.local_db_path, active_dir_path, archive_dir_path),
            base_snapshot_path=_map_session_path(session.base_snapshot_path, active_dir_path, archive_dir_path),
            settings_snapshot_path=_map_optional_session_path(session.settings_snapshot_path, active_dir_path, archive_dir_path),
            last_dry_run_report_path=_map_optional_session_path(session.last_dry_run_report_path, active_dir_path, archive_dir_path),
            last_merge_report_path=archived_report_path,
            remote_backup_path=result.remote_backup_path,
            local_backup_path=archived_local_backup_path,
            merge_result="success",
            final_remote_last_change_id=result.remote_last_change_id_after,
            final_remote_hash=result.final_remote_hash,
            last_merge_error=None,
        )
        self.store.write_active_session(updated)
        return updated

    def archive_emergency_session(self, session_id: str) -> str:
        source_dir = active_session_dir(self.store.resolve_root(), session_id)
        target_dir = _unique_archive_dir(self.store.resolve_root(), session_id)
        os.makedirs(os.path.dirname(target_dir), exist_ok=True)
        shutil.move(source_dir, target_dir)
        return target_dir

    def create_fresh_standby_after_merge(self, context: DbRuntimeContext) -> dict[str, Any]:
        try:
            manager = EmergencyStandbyManager(
                root=self.store.resolve_root(),
                source_medical_db_path=context.medical_db_path,
                source_settings_db_path=context.settings_db_path,
                settings_required=True,
                is_safe_to_refresh=lambda: True,
                store=self.store,
            )
            result = manager.create_or_refresh_standby(forced=True)
            return {"ok": bool(result.ok), "status": result.status, "reason": result.reason}
        except Exception as exc:
            return {"ok": False, "status": "warning", "reason": str(exc)}

    def _load_marker(
        self,
        session: EmergencySessionMetadata,
        marker_path: str,
        blockers: list[dict[str, str]],
    ) -> dict[str, Any]:
        try:
            marker = load_merge_ready_marker(marker_path)
            ok, reason = validate_merge_ready_marker(marker, session, ttl_sec=self.marker_ttl_sec)
            if not ok:
                blockers.append(_blocker("merge_ready_marker_invalid", reason))
            return marker
        except Exception as exc:
            blockers.append(_blocker("merge_ready_marker_required", str(exc)))
            return {}

    def _load_dry_run_report(
        self,
        session: EmergencySessionMetadata,
        dry_run_report_path: str | None,
        blockers: list[dict[str, str]],
    ) -> dict[str, Any]:
        if not dry_run_report_path:
            blockers.append(_blocker("dry_run_ready_report_required", "Сначала нужна проверка объединения."))
            return {}
        if not _path_is_under(dry_run_report_path, self._session_logs_dir(session.emergency_session_id)):
            blockers.append(_blocker("dry_run_report_invalid", "dry-run report must be under active session logs"))
            return {}
        try:
            report = read_json_file(dry_run_report_path)
        except Exception as exc:
            blockers.append(_blocker("dry_run_ready_report_required", str(exc)))
            return {}
        self._validate_dry_report_payload(session, report, blockers)
        return report

    def _validate_dry_report_payload(
        self,
        session: EmergencySessionMetadata,
        report: dict[str, Any],
        blockers: list[dict[str, str]],
    ) -> None:
        if str(report.get("emergency_session_id") or "") != session.emergency_session_id:
            blockers.append(_blocker("dry_run_report_invalid", "dry-run report session id mismatch"))
        if str(report.get("result_status") or "") not in {"ready_mode_a", "ready_emergency_authoritative"}:
            blockers.append(_blocker("dry_run_report_not_ready", "Сначала нужна проверка объединения."))
        if str(report.get("merge_mode") or "") not in {"remote_unchanged_mode_a", "remote_changed_emergency_authoritative"}:
            blockers.append(_blocker("dry_run_report_not_mode_a", "dry-run report is not an allowed emergency merge mode"))
        if _int_value(report.get("base_last_change_id"), default=-1) != int(session.base_last_change_id or 0):
            blockers.append(_blocker("dry_run_report_invalid", "dry-run base_last_change_id mismatch"))
        if _int_value(report.get("remote_last_change_id"), default=-1) < int(session.base_last_change_id or 0):
            blockers.append(_blocker("dry_run_report_invalid", "dry-run remote_last_change_id is below base"))
        if report.get("blockers"):
            blockers.append(_blocker("dry_run_report_invalid", "dry-run report contains blockers"))
        if _report_expired(report.get("created_at"), self.dry_run_report_ttl_sec):
            blockers.append(_blocker("dry_run_report_expired", "Сначала нужна проверка объединения."))
        report_version = str(report.get("app_version") or APP_VERSION)
        if _compare_client_versions(APP_VERSION, report_version) < 0:
            blockers.append(_blocker("dry_run_report_app_version_incompatible", "dry-run report app_version is newer than current client"))

    def _settings_warnings(self, session: EmergencySessionMetadata, remote_payload: dict[str, Any]) -> list[str]:
        warnings: list[str] = []
        try:
            snapshot_hash = compute_file_hash(str(session.settings_snapshot_path))
            remote_hash = str((remote_payload.get("settings") or {}).get("file_hash") or "")
            if remote_hash and snapshot_hash != remote_hash:
                warnings.append("settings snapshot differs from remote settings DB; remote settings DB was not replaced")
        except Exception:
            warnings.append("settings snapshot hash could not be compared; remote settings DB was not replaced")
        return warnings

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

    def _recheck_session_locks(self, context: DbRuntimeContext) -> dict[str, Any]:
        return self._dry_run_service.check_session_locks(context)

    def _validate_remote_unchanged(self, session: EmergencySessionMetadata, context: DbRuntimeContext) -> dict[str, Any]:
        remote_validation = validate_medical_db_snapshot(context.medical_db_path)
        local_validation = validate_medical_db_snapshot(session.local_db_path)
        if not remote_validation.ok:
            return {"ok": False, "code": "remote_invalid", "reason": remote_validation.reason}
        identity_error = validate_remote_identity_error(session, context.medical_db_path, remote_validation)
        if identity_error:
            return {"ok": False, "code": "blocked_remote_identity_mismatch", "reason": identity_error}
        if not local_validation.ok:
            return {"ok": False, "code": "local_invalid", "reason": local_validation.reason}
        remote_last = int(remote_validation.last_change_id or 0)
        base_last = int(session.base_last_change_id or 0)
        if remote_last < base_last:
            return {
                "ok": False,
                "code": "blocked_remote_inconsistent",
                "reason": "remote_last_change_id is below base_last_change_id",
                "remote_last_change_id": remote_last,
                "local_last_change_id": int(local_validation.last_change_id or 0),
            }
        return {
            "ok": True,
            "remote_last_change_id": remote_last,
            "local_last_change_id": int(local_validation.last_change_id or 0),
            "remote_hash": remote_validation.file_hash,
            "local_hash": local_validation.file_hash,
            "remote_changed_after_base": remote_last > base_last,
        }

    def _sqlite_backup(
        self,
        source_path: str,
        backup_path: str,
        *,
        invalid_dir: str | None,
        source: str,
        validate: bool = True,
    ) -> str:
        os.makedirs(os.path.dirname(backup_path), exist_ok=True)
        conn = None
        try:
            conn = sqlite3.connect(
                f"file:{os.path.abspath(source_path)}?mode=ro",
                uri=True,
                check_same_thread=False,
                isolation_level=None,
                timeout=5.0,
            )
            configure_connection(conn, readonly=True, profile="network")
            return backup_connection(
                conn,
                backup_path,
                invalid_dir=invalid_dir,
                validate=validate,
                lock_path=None,
                source=source,
            )
        finally:
            if conn is not None:
                conn.close()

    def _quarantine_existing_temp(self, temp_path: str) -> None:
        if not os.path.exists(temp_path):
            return
        _ = validate_medical_db_snapshot(temp_path)
        self._quarantine_temp(temp_path, "stale_existing")

    def _quarantine_temp(self, path: str, reason: str) -> str:
        if not path or not os.path.exists(path):
            return ""
        target = f"{path}.{reason}.{_stamp()}.{uuid.uuid4().hex[:8]}.quarantine"
        try:
            _replace_file_with_retry(path, target)
            return target
        except OSError:
            return ""

    def _latest_dry_run_report_path(self, session_id: str) -> str:
        logs_dir = self._session_logs_dir(session_id)
        if not os.path.isdir(logs_dir):
            return ""
        reports = sorted(
            os.path.join(logs_dir, name)
            for name in os.listdir(logs_dir)
            if name.startswith("emergency_merge_dry_run_") and name.endswith(".json")
        )
        return reports[-1] if reports else ""

    def _merge_report_path(self, session_id: str) -> str:
        return os.path.join(
            self._session_logs_dir(session_id),
            f"emergency_merge_mode_a_{_stamp()}.json",
        )

    def _session_logs_dir(self, session_id: str) -> str:
        return os.path.join(active_session_dir(self.store.resolve_root(), session_id), "logs")

    def _clear_success_markers(self, session: EmergencySessionMetadata) -> None:
        clear_merge_ready_marker(self.store.resolve_root(), session.emergency_session_id)
        startup_request = runtime_outage_startup_request_path(self.store.resolve_root())
        try:
            os.remove(startup_request)
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def _mark_merge_failed_if_possible(
        self,
        session: EmergencySessionMetadata | None,
        result: EmergencyModeAMergeResult,
    ) -> None:
        if session is None:
            return
        try:
            current = self.store.read_active_session(session.emergency_session_id)
            updated = replace(
                current,
                status="merge_failed",
                ended_at=_now_text(),
                last_merge_error=result.error or result.error_code or "merge failed",
                last_merge_report_path=result.report_path,
                merge_attempt_count=int(current.merge_attempt_count or 0) + 1,
                merge_result=result.result_status,
            )
            self.store.write_active_session(updated)
        except Exception:
            pass

    def _build_success_result(
        self,
        *,
        session: EmergencySessionMetadata,
        prereq: _Prerequisites,
        report_path: str,
        backups: dict[str, str],
        temp_info: dict[str, Any],
        replacement: dict[str, str],
        final_validation: dict[str, Any],
        remote_before: dict[str, Any],
        remote_after: SnapshotValidationResult,
        locks_acquired: dict[str, Any],
        session_locks: dict[str, Any],
        fresh_standby: dict[str, Any],
        started_at: str,
        started: float,
    ) -> EmergencyModeAMergeResult:
        warnings = list(prereq.warnings)
        if bool(remote_before.get("remote_changed_after_base")):
            warnings.append("remote medical DB changed after emergency base; local emergency medical DB replaced it")
            warnings.append("remote settings DB was not replaced")
        if not fresh_standby.get("ok"):
            warnings.append(f"fresh standby creation warning: {fresh_standby.get('reason')}")
        return self._result(
            session_id=session.emergency_session_id,
            status=EmergencyModeAMergeStatus.SUCCESS,
            error_code="",
            error=None,
            report_path=report_path,
            marker_path=prereq.marker_path,
            dry_run_report_path=prereq.dry_run_report_path,
            remote_backup_path=backups["remote_backup_path"],
            local_backup_path=backups["local_backup_path"],
            temp_db_path=temp_info["temp_db_path"],
            temp_db_hash=temp_info["temp_db_hash"],
            pre_merge_remote_path=replacement.get("pre_merge_remote_path", ""),
            final_remote_hash=str(final_validation.get("file_hash") or remote_after.file_hash or ""),
            post_quick_check_status=str(final_validation.get("quick_check") or remote_after.reason or ""),
            post_integrity_check_status=str(final_validation.get("integrity_check") or ""),
            base_last_change_id=int(session.base_last_change_id or 0),
            local_last_change_id=int(remote_before["local_last_change_id"]),
            remote_last_change_id_before=int(remote_before["remote_last_change_id"]),
            remote_last_change_id_after=int(remote_after.last_change_id or 0),
            applied_change_count_estimate=_applied_change_count(prereq.dry_run_report),
            changed_tables_summary=_changed_tables_summary(prereq.dry_run_report),
            locks_acquired=locks_acquired,
            session_locks_status=session_locks,
            fresh_standby_status=fresh_standby,
            warnings=warnings,
            started_at=started_at,
            started=started,
        )

    def _blocked_result_from_status(
        self,
        session: EmergencySessionMetadata,
        prereq: _Prerequisites,
        code: str,
        reason: str,
        report_path: str,
        started_at: str,
        started: float,
        *,
        locks_acquired: dict[str, Any] | None = None,
        session_locks_status: dict[str, Any] | None = None,
        remote_last_change_id_before: int = 0,
    ) -> EmergencyModeAMergeResult:
        return self._result(
            session_id=session.emergency_session_id,
            status=EmergencyModeAMergeStatus.BLOCKED,
            error_code=code,
            error=reason,
            blockers=[_blocker(code, reason)],
            warnings=prereq.warnings,
            report_path=report_path,
            marker_path=prereq.marker_path,
            dry_run_report_path=prereq.dry_run_report_path,
            base_last_change_id=int(session.base_last_change_id or 0),
            local_last_change_id=int((prereq.local_validation.get("local") or {}).get("last_change_id") or 0),
            remote_last_change_id_before=remote_last_change_id_before,
            changed_tables_summary=_changed_tables_summary(prereq.dry_run_report),
            applied_change_count_estimate=_applied_change_count(prereq.dry_run_report),
            locks_acquired=locks_acquired or {},
            session_locks_status=session_locks_status or {},
            started_at=started_at,
            started=started,
        )

    def _result(
        self,
        *,
        session_id: str,
        status: EmergencyModeAMergeStatus,
        error_code: str,
        error: str | None,
        report_path: str,
        started_at: str,
        started: float,
        blockers: list[dict[str, str]] | None = None,
        warnings: list[str] | None = None,
        marker_path: str = "",
        dry_run_report_path: str = "",
        remote_backup_path: str = "",
        local_backup_path: str = "",
        temp_db_path: str = "",
        temp_db_hash: str = "",
        pre_merge_remote_path: str = "",
        final_remote_hash: str = "",
        post_quick_check_status: str = "",
        post_integrity_check_status: str | None = None,
        base_last_change_id: int = 0,
        local_last_change_id: int = 0,
        remote_last_change_id_before: int = 0,
        remote_last_change_id_after: int = 0,
        applied_change_count_estimate: int = 0,
        changed_tables_summary: dict[str, Any] | None = None,
        locks_acquired: dict[str, Any] | None = None,
        session_locks_status: dict[str, Any] | None = None,
        rollback_status: str | None = None,
        archive_path: str = "",
        fresh_standby_status: dict[str, Any] | None = None,
    ) -> EmergencyModeAMergeResult:
        finished_at = _now_text()
        status_value = status.value
        message = MERGE_SUCCESS_MESSAGE if status is EmergencyModeAMergeStatus.SUCCESS else MERGE_FAILURE_MESSAGE
        return EmergencyModeAMergeResult(
            ok=status is EmergencyModeAMergeStatus.SUCCESS,
            result_status=status_value,
            error_code=error_code,
            error=error,
            session_id=session_id,
            report_path=report_path,
            user_message=message,
            blockers=blockers or [],
            warnings=warnings or [],
            marker_path=marker_path,
            dry_run_report_path=dry_run_report_path,
            remote_backup_path=remote_backup_path,
            local_backup_path=local_backup_path,
            temp_db_path=temp_db_path,
            temp_db_hash=temp_db_hash,
            pre_merge_remote_path=pre_merge_remote_path,
            final_remote_hash=final_remote_hash,
            post_quick_check_status=post_quick_check_status,
            post_integrity_check_status=post_integrity_check_status,
            base_last_change_id=int(base_last_change_id or 0),
            local_last_change_id=int(local_last_change_id or 0),
            remote_last_change_id_before=int(remote_last_change_id_before or 0),
            remote_last_change_id_after=int(remote_last_change_id_after or 0),
            applied_change_count_estimate=int(applied_change_count_estimate or 0),
            changed_tables_summary=changed_tables_summary or {},
            locks_acquired=locks_acquired or {},
            session_locks_status=session_locks_status or {},
            rollback_status=rollback_status,
            archive_path=archive_path,
            fresh_standby_status=fresh_standby_status or {},
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )


def run_emergency_mode_a_merge(
    session_id: str,
    *,
    dry_run_report_path: str | None = None,
    marker_path: str | None = None,
    role: str = "nurse",
) -> EmergencyModeAMergeResult:
    service = EmergencyModeAMergeService(role=role)
    return service.run_merge(session_id, dry_run_report_path=dry_run_report_path, marker_path=marker_path)


def _infer_emergency_root(runtime_context: DbRuntimeContext | None) -> str | None:
    baza_dir = str(getattr(runtime_context, "baza_dir", "") or "")
    if not baza_dir:
        return None
    active_dir_path = os.path.dirname(os.path.abspath(os.path.normpath(baza_dir)))
    if os.path.basename(active_dir_path).lower() == "active":
        return os.path.dirname(active_dir_path)
    return None


def _optional_path(path: str | None) -> str | None:
    if not path:
        return None
    return os.path.abspath(os.path.normpath(str(path)))


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def _safe_id(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(value or "session"))
    return safe[:96] or "session"


def _short_id(value: str) -> str:
    safe = _safe_id(value)
    return safe[-10:] if len(safe) > 10 else safe


def _blocker(code: str, reason: str) -> dict[str, str]:
    return {"code": str(code or "blocked"), "reason": str(reason or "")}


def _prefix_blockers(blockers: list[dict[str, str]], fallback: str) -> list[dict[str, str]]:
    if not blockers:
        return [_blocker(fallback, fallback)]
    return [_blocker(str(item.get("code") or fallback), str(item.get("reason") or fallback)) for item in blockers]


def _first_blocker_code(blockers: list[dict[str, str]], fallback: str) -> str:
    return str((blockers[0] if blockers else {}).get("code") or fallback)


def _first_blocker_reason(blockers: list[dict[str, str]]) -> str:
    return str((blockers[0] if blockers else {}).get("reason") or "")


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _int_value(value: Any, *, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _report_expired(created_at: Any, ttl_sec: int) -> bool:
    created = _parse_time(created_at)
    if created is None:
        return True
    return datetime.now(timezone.utc) - created > timedelta(seconds=max(1, int(ttl_sec or 1)))


def _path_is_under(path: str, root: str) -> bool:
    try:
        path_abs = os.path.normcase(os.path.abspath(path))
        root_abs = os.path.normcase(os.path.abspath(root))
        return os.path.commonpath([path_abs, root_abs]) == root_abs
    except Exception:
        return False


def _changed_tables_summary(report: dict[str, Any]) -> dict[str, Any]:
    value = report.get("changed_tables_summary")
    return dict(value) if isinstance(value, dict) else {}


def _applied_change_count(report: dict[str, Any]) -> int:
    local_last = int(report.get("local_last_change_id") or 0)
    base_last = int(report.get("base_last_change_id") or 0)
    return max(0, local_last - base_last)


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
        medical_backups_root_dir=os.path.join(effective_baza, "backups"),
        medical_invalid_backups_dir=os.path.join(effective_baza, "backup_health", "invalid_backups"),
        medical_backup_health_dir=os.path.join(effective_baza, "backup_health"),
        medical_quarantine_dir=os.path.join(effective_baza, "quarantine"),
        medical_snapshots_dir=os.path.join(effective_baza, "snapshots"),
        medical_logs_dir=os.path.join(effective_baza, "logs"),
        recovery_lock_path=os.path.join(effective_baza, "locks", "recovery.lock"),
        session_locks_dir=os.path.join(effective_baza, "session_locks"),
        settings_db_path=effective_settings,
        settings_db_lock_path=os.path.join(os.path.dirname(effective_settings), "settings.db.lock"),
        settings_backup_health_dir=os.path.join(os.path.dirname(effective_settings), "backup_health"),
        medical_db_rotation_lock_path=os.path.join(os.path.dirname(effective_medical), "db_rotation.lock"),
        medical_client_policy_path=os.path.join(effective_baza, "config", "client_policy.json"),
    )


def _public_lock_status(status: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in status.items() if key != "_locks"}


def _run_integrity_check_path(path: str) -> tuple[bool, str]:
    conn = None
    try:
        conn = sqlite3.connect(
            f"file:{os.path.abspath(path)}?mode=ro",
            uri=True,
            check_same_thread=False,
            isolation_level=None,
            timeout=5.0,
        )
        configure_connection(conn, readonly=True, profile="network")
        return run_integrity_check(conn)
    except Exception as exc:
        return False, str(exc)
    finally:
        if conn is not None:
            conn.close()


def _replace_file_with_retry(source: str, target: str) -> None:
    last_error: OSError | None = None
    for attempt in range(20):
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            last_error = exc
            if getattr(exc, "winerror", None) not in {5, 32} or attempt >= 19:
                raise
            time.sleep(0.05)
    if last_error is not None:
        raise last_error


def _unique_pre_merge_path(remote_db_path: str, session_id: str) -> str:
    directory = os.path.dirname(remote_db_path)
    short_session = _short_id(session_id)
    for _ in range(20):
        candidate = os.path.join(
            directory,
            f"rao_j.pre_{short_session}_{_stamp()}_{uuid.uuid4().hex[:6]}.db",
        )
        if not os.path.exists(candidate):
            return candidate
    raise EmergencyModeAMergeError("could not allocate pre-merge remote path")


def _unique_archive_dir(root: str, session_id: str) -> str:
    base = archived_session_dir(root, session_id)
    if not os.path.exists(base):
        return base
    for _ in range(20):
        candidate = f"{base}_{_stamp()}_{uuid.uuid4().hex[:8]}"
        if not os.path.exists(candidate):
            return candidate
    raise EmergencyModeAMergeError("could not allocate archive session directory")


def _map_session_path(path: str, active_dir_path: str, archive_dir_path: str) -> str:
    if not path:
        return path
    try:
        path_abs = os.path.abspath(path)
        active_abs = os.path.abspath(active_dir_path)
        if os.path.commonpath([os.path.normcase(path_abs), os.path.normcase(active_abs)]) == os.path.normcase(active_abs):
            rel_path = os.path.relpath(path_abs, active_abs)
            return os.path.abspath(os.path.join(archive_dir_path, rel_path))
    except Exception:
        pass
    return path


def _map_optional_session_path(path: str | None, active_dir_path: str, archive_dir_path: str) -> str | None:
    if path is None:
        return None
    return _map_session_path(path, active_dir_path, archive_dir_path)
