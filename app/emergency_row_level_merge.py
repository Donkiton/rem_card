from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Any

from rem_card.app.emergency_merge_mode_a import EmergencyModeAMergeService
from rem_card.app.emergency_metadata import EmergencySessionMetadata, atomic_write_json
from rem_card.app.emergency_paths import active_session_dir
from rem_card.app.emergency_validation import validate_medical_db_snapshot
from rem_card.app.sqlite_uri import build_sqlite_file_uri
from rem_card.app.sqlite_shared import configure_connection, restore_database, run_integrity_check, run_quick_check


ROW_LEVEL_MERGE_REPORT_VERSION = 1
ROW_LEVEL_MERGE_MODE = "emergency_authoritative_row_level_merge"

MERGE_SUCCESS_MESSAGE = (
    "Объединение завершено.\n"
    "Локальные аварийные изменения перенесены в сетевую базу без замены файла БД."
)
MERGE_FAILURE_MESSAGE = (
    "Объединение не завершено.\n"
    "Аварийная база сохранена.\n"
    "Работа остаётся в аварийном режиме."
)

REMCARD_MERGE_TABLE_ORDER: tuple[str, ...] = (
    "patients",
    "admissions",
    "beds",
    "operations",
    "ivl_episodes",
    "transfusions",
    "clinical_events",
    "devices",
    "respiratory_support",
    "lab_data",
    "drugs",
    "vitals",
    "vital_settings",
    "fluids",
    "orders",
    "administrations",
    "patient_status_events",
    "order_audit_log",
    "diet_templates",
    "diet_plan",
    "diet_plan_versions",
    "oral_intake_events",
    "procedures",
    "lab_orders",
    "procedure_consents",
    "procedure_cvc",
    "procedure_lumbar_puncture",
    "procedure_transfusion",
)

PROTECTED_OPBLOCK_TABLES: frozenset[str] = frozenset(
    {
        "operating_tables",
        "operation_cases",
        "operation_table_assignments",
        "operblock_timeline_events",
        "opblock_offline_case_map",
    }
)

NON_REPLAY_TABLES: frozenset[str] = frozenset(
    {
        "meta",
        "change_log",
        "sync_applied_ops",
        "schema_migrations",
        "medical_audit_log",
        "sqlite_sequence",
    }
)

FK_REMAP_COLUMNS: dict[str, str] = {
    "patient_id": "patients",
    "admission_id": "admissions",
    "current_admission_id": "admissions",
    "order_id": "orders",
    "source_order_id": "orders",
    "source_admin_id": "administrations",
    "procedure_id": "procedures",
    "template_id": "diet_templates",
    "ivl_episode_id": "ivl_episodes",
    "event_id": "clinical_events",
}


@dataclass(frozen=True)
class RowOperation:
    table: str
    action: str
    pk_columns: tuple[str, ...]
    pk_values: tuple[Any, ...]
    row: dict[str, Any] = field(default_factory=dict)
    base_row: dict[str, Any] = field(default_factory=dict)
    remote_row: dict[str, Any] = field(default_factory=dict)
    insert_new_primary_key: bool = False
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    # Stable review grouping.  These values are deliberately stored on every
    # operation rather than inferred by the UI from a table-specific FK.
    admission_id: int | None = None
    patient_id: int | None = None


@dataclass(frozen=True)
class RowMergePlan:
    operations: list[RowOperation] = field(default_factory=list)
    blockers: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    changed_tables_summary: dict[str, dict[str, int]] = field(default_factory=dict)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    protected_summary: dict[str, Any] = field(default_factory=dict)
    skipped_tables: list[dict[str, str]] = field(default_factory=list)

    @property
    def applied_change_count_estimate(self) -> int:
        return len(self.operations)

    @property
    def ok(self) -> bool:
        return not self.blockers


@dataclass(frozen=True)
class EmergencyRowLevelMergeResult:
    ok: bool
    result_status: str
    error_code: str = ""
    error: str | None = None
    session_id: str = ""
    report_path: str = ""
    user_message: str = MERGE_FAILURE_MESSAGE
    blockers: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    marker_path: str = ""
    dry_run_report_path: str = ""
    remote_backup_path: str = ""
    local_backup_path: str = ""
    pre_merge_remote_path: str = ""
    final_remote_hash: str = ""
    post_quick_check_status: str = ""
    post_integrity_check_status: str | None = None
    post_foreign_key_check_status: str | None = None
    base_last_change_id: int = 0
    local_last_change_id: int = 0
    remote_last_change_id_before: int = 0
    remote_last_change_id_after: int = 0
    applied_change_count_estimate: int = 0
    changed_tables_summary: dict[str, Any] = field(default_factory=dict)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    protected_summary: dict[str, Any] = field(default_factory=dict)
    skipped_tables: list[dict[str, str]] = field(default_factory=list)
    locks_acquired: dict[str, Any] = field(default_factory=dict)
    session_locks_status: dict[str, Any] = field(default_factory=dict)
    rollback_status: str | None = None
    archive_path: str = ""
    fresh_standby_status: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    requires_recovery: bool | None = None

    # Compatibility with PendingEmergencyMergeResult and metadata mapping.
    @property
    def temp_db_path(self) -> str:
        return ""

    @property
    def temp_db_hash(self) -> str:
        return ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EmergencyRowLevelMergeService:
    def __init__(self, **kwargs: Any):
        self._legacy = EmergencyModeAMergeService(**kwargs)
        self.store = self._legacy.store

    def run_merge(
        self,
        session_id: str,
        dry_run_report_path: str | None = None,
        marker_path: str | None = None,
        selected_admission_ids: list[int] | None = None,
        expected_plan_digest: str | None = None,
    ) -> EmergencyRowLevelMergeResult:
        started_at = _now_text()
        started = time.perf_counter()
        report_path = self._merge_report_path(session_id)
        locks: list[Any] = []
        session: EmergencySessionMetadata | None = None
        self._outcome_uncertain = True
        try:
            if selected_admission_ids is None or not expected_plan_digest:
                result = self._result(
                    session_id=session_id, status="blocked", error_code="review_required",
                    error="Для объединения требуется подтверждённый выбор карт и контрольная сумма предпросмотра.",
                    blockers=[_blocker("review_required", "Для объединения требуется подтверждённый выбор карт и контрольная сумма предпросмотра.")],
                    report_path=report_path, marker_path=marker_path or "", dry_run_report_path=dry_run_report_path or "",
                    started_at=started_at, started=started,
                )
                self.write_merge_report(result)
                return result
            session = self.store.read_active_session(session_id)
            uncertain_previous_commit = (
                bool(getattr(session, "merge_recovery_required", False))
                or session.status in {"merging", "merge_failed"}
            )
            self._outcome_uncertain = uncertain_previous_commit
            self._receipt_presence = None
            recovered = self.recover_committed_merge(session_id, expected_plan_digest, report_path, started_at, started)
            if recovered is not None:
                return recovered
            if self._receipt_presence is False:
                self._outcome_uncertain = False
            if self._receipt_presence is True or (uncertain_previous_commit and self._receipt_presence is not False):
                result = self._result(
                    session_id=session_id, status="blocked", error_code="merge_outcome_unknown",
                    error="Не удалось установить результат предыдущего переноса. Локальную работу пока нельзя продолжить.",
                    blockers=[_blocker("merge_outcome_unknown", "Дождитесь доступа к основной базе для проверки результата переноса.")],
                    report_path=report_path, marker_path=marker_path or "", dry_run_report_path=dry_run_report_path or "",
                    started_at=started_at, started=started,
                )
                return self._record_blocked(session, result)
            prereq = self._legacy.load_and_validate_prerequisites(session_id, dry_run_report_path, marker_path)
            session = prereq.session
            if not prereq.ok or session is None or prereq.context is None:
                result = self._result(
                    session_id=session_id,
                    status="blocked",
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

            return self._run_prepared_merge(
                session, prereq, selected_admission_ids, expected_plan_digest,
                report_path, started_at, started,
            )
        except Exception as exc:
            result = self._result(
                session_id=session_id,
                status="failed",
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

    def validate_final_remote_db(self, path: str) -> dict[str, Any]:
        validation = validate_medical_db_snapshot(path)
        if not validation.ok:
            return {"ok": False, "reason": validation.reason, "quick_check": validation.reason}
        conn = _connect(path, readonly=True)
        try:
            quick_ok, quick_reason = run_quick_check(conn)
            if not quick_ok:
                return {"ok": False, "reason": f"quick_check failed: {quick_reason}", "quick_check": quick_reason}
            integrity_ok, integrity_reason = run_integrity_check(conn)
            if not integrity_ok:
                return {
                    "ok": False,
                    "reason": f"integrity_check failed: {integrity_reason}",
                    "quick_check": quick_reason,
                    "integrity_check": integrity_reason,
                }
            fk_issues = _foreign_key_issues(conn)
            if fk_issues:
                return {
                    "ok": False,
                    "reason": f"foreign_key_check failed: {fk_issues[:5]}",
                    "quick_check": quick_reason,
                    "integrity_check": integrity_reason,
                    "foreign_key_check": fk_issues[:20],
                }
            return {
                "ok": True,
                "reason": "ok",
                "quick_check": quick_reason,
                "integrity_check": integrity_reason,
                "foreign_key_check": "ok",
                "last_change_id": int(validation.last_change_id or 0),
                "file_hash": validation.file_hash,
            }
        finally:
            conn.close()

    def recover_committed_merge(
        self,
        session_id: str,
        approved_plan_digest: str,
        report_path: str | None = None,
        started_at: str | None = None,
        started: float | None = None,
    ) -> EmergencyRowLevelMergeResult | None:
        """Finalize a committed transaction before any stale-review checks.

        A receipt is authoritative only after obtaining the normal write locks
        and re-validating the configured remote identity.  ``None`` means no
        receipt was found, so the ordinary reviewed merge path may continue.
        """
        if not hasattr(self._legacy, "_network_context"):
            return None
        started_at = started_at or _now_text()
        started = started if started is not None else time.perf_counter()
        report_path = report_path or self._merge_report_path(session_id)
        locks: list[Any] = []
        try:
            session = self.store.read_active_session(session_id)
            context = self._legacy._network_context()
            if context is None:
                return None
            from rem_card.app.emergency_remote_identity import validate_remote_identity_error
            identity_error = validate_remote_identity_error(session, context.medical_db_path)
            if identity_error:
                return None
            lock_status = self._legacy.acquire_merge_locks(context)
            if not lock_status.get("ok"):
                return None
            locks = list(lock_status.get("_locks") or [])
            session_locks = self._legacy._recheck_session_locks(context)
            if not session_locks.get("ok"):
                return None
            self._receipt_presence = has_merge_receipt(context.medical_db_path, session_id, approved_plan_digest)
            if not self._receipt_presence:
                return None
            self._outcome_uncertain = True
            final_validation = self.validate_final_remote_db(context.medical_db_path)
            if not final_validation.get("ok"):
                return self._result(
                    session_id=session_id, status="blocked", error_code="receipt_validation_failed",
                    error=str(final_validation.get("reason") or "receipt validation failed"), report_path=report_path,
                    blockers=[_blocker("receipt_validation_failed", str(final_validation.get("reason") or ""))],
                    locks_acquired=_public_lock_status(lock_status), session_locks_status=session_locks,
                    started_at=started_at, started=started,
                )
            fresh_standby = self._legacy.create_fresh_standby_after_merge(context)
            result = self._result(
                session_id=session_id, status="success", error_code="", error=None, report_path=report_path,
                final_remote_hash=str(final_validation.get("file_hash") or ""), post_quick_check_status=str(final_validation.get("quick_check") or ""),
                post_integrity_check_status=str(final_validation.get("integrity_check") or ""), post_foreign_key_check_status=str(final_validation.get("foreign_key_check") or ""),
                remote_last_change_id_after=int(final_validation.get("last_change_id") or 0),
                warnings=["Подтверждённое объединение уже было зафиксировано; выполнено локальное завершение."],
                locks_acquired=_public_lock_status(lock_status), session_locks_status=session_locks,
                fresh_standby_status=fresh_standby, started_at=started_at, started=started,
            )
            return self._finalize_success(session, result)
        except Exception:
            # Receipt recovery must not turn a transient identity/lock problem
            # into a new clinical write.  The normal path reports its blocker.
            return None
        finally:
            for lock in reversed(locks):
                lock.release()

    def _run_prepared_merge(self, session, prereq, selected, digest, report_path, started_at, started):
        lock_status = self._legacy.acquire_merge_locks(prereq.context)
        if not lock_status.get("ok"):
            return self._record_blocked(session, self._blocked_result(
                session, prereq, "locks_unavailable",
                str(lock_status.get("reason") or "merge locks unavailable"), report_path, started_at, started,
                locks_acquired=_public_lock_status(lock_status),
            ))
        locks = list(lock_status.get("_locks") or [])
        try:
            session_locks = self._legacy._recheck_session_locks(prereq.context)
            if not session_locks.get("ok"):
                return self._record_blocked(session, self._blocked_result(
                    session, prereq, "active_session_lock", "role/session lock is active", report_path, started_at, started,
                    locks_acquired=_public_lock_status(lock_status), session_locks_status=session_locks,
                ))
            remote = self._legacy._validate_remote_unchanged(session, prereq.context)
            if not remote.get("ok"):
                return self._record_blocked(session, self._blocked_result(
                    session, prereq, str(remote.get("code") or "remote_invalid"),
                    str(remote.get("reason") or "remote validation failed"), report_path, started_at, started,
                    locks_acquired=_public_lock_status(lock_status), session_locks_status=session_locks,
                    remote_last_change_id_before=int(remote.get("remote_last_change_id") or 0),
                ))
            return self._run_locked_plan(
                session, prereq, selected, digest, remote, lock_status, session_locks, report_path, started_at, started,
            )
        finally:
            for lock in reversed(locks):
                lock.release()

    def _run_locked_plan(self, session, prereq, selected, digest, remote, lock_status, session_locks, report_path, started_at, started):
        if has_merge_receipt(prereq.context.medical_db_path, session.emergency_session_id, digest):
            return self._finalize_receipt(session, prereq, remote, lock_status, session_locks, report_path, started_at, started)
        plan = build_row_merge_plan(
            base_db_path=session.base_snapshot_path, local_db_path=session.local_db_path,
            remote_db_path=prereq.context.medical_db_path, selected_admission_ids=selected, authoritative=True,
        )
        blocked = self._reviewed_plan_block(session, prereq, plan, digest, remote, lock_status, session_locks, report_path, started_at, started)
        if blocked is not None:
            return blocked
        return self._apply_checked_plan(session, prereq, plan, digest, remote, lock_status, session_locks, report_path, started_at, started)

    def _reviewed_plan_block(self, session, prereq, plan, digest, remote, lock_status, session_locks, report_path, started_at, started):
        if not plan.ok:
            result = self._blocked_result(
                session, prereq, "row_merge_plan_blocked", _first_blocker_reason(plan.blockers), report_path, started_at, started,
                blockers=plan.blockers, warnings=[*prereq.warnings, *plan.warnings],
                locks_acquired=_public_lock_status(lock_status), session_locks_status=session_locks,
                remote_last_change_id_before=int(remote.get("remote_last_change_id") or 0), plan=plan,
            )
            return self._record_blocked(session, result)
        actual = plan_digest(plan)
        if actual == digest:
            return None
        result = self._blocked_result(
            session, prereq, "plan_digest_mismatch", "Состав данных изменился после предпросмотра; требуется повторное подтверждение.",
            report_path, started_at, started,
            blockers=[_blocker("plan_digest_mismatch", "Состав данных изменился после предпросмотра; требуется повторное подтверждение.", expected=digest, actual=actual)],
            warnings=[*prereq.warnings, *plan.warnings], locks_acquired=_public_lock_status(lock_status),
            session_locks_status=session_locks, remote_last_change_id_before=int(remote.get("remote_last_change_id") or 0), plan=plan,
        )
        self.write_merge_report(result)
        return result

    def _finalize_receipt(self, session, prereq, remote, lock_status, session_locks, report_path, started_at, started):
        final = self.validate_final_remote_db(prereq.context.medical_db_path)
        if not final.get("ok"):
            result = self._blocked_result(session, prereq, "receipt_validation_failed", str(final.get("reason") or "receipt validation failed"), report_path, started_at, started)
            self.write_merge_report(result)
            return result
        fresh = self._legacy.create_fresh_standby_after_merge(prereq.context)
        result = self._success_result(session, prereq, {}, final, remote, lock_status, session_locks, report_path, started_at, started, fresh, warnings=[*prereq.warnings, "Подтверждённое объединение уже было зафиксировано; выполнено локальное завершение."])
        return self._finalize_success(session, result)

    def _apply_checked_plan(self, session, prereq, plan, digest, remote, lock_status, session_locks, report_path, started_at, started):
        backups = self._legacy.create_pre_merge_backups(session, prereq.context)
        # Persist uncertainty before COMMIT can happen, including process/power loss.
        current = self.store.read_active_session(session.emergency_session_id)
        self.store.write_active_session(replace(current, status="merging", merge_recovery_required=True))
        self._outcome_uncertain = True
        applied = apply_row_merge_plan(prereq.context.medical_db_path, plan, session_id=session.emergency_session_id, approved_plan_digest=digest)
        final = self.validate_final_remote_db(prereq.context.medical_db_path)
        if not final.get("ok"):
            rollback = self._restore_remote_backup(prereq.context.medical_db_path, backups["remote_backup_path"], reason=str(final.get("reason") or "final validation failed"))
            result = self._rollback_result(session, prereq, plan, final, remote, lock_status, session_locks, report_path, started_at, started, backups, rollback)
            return self._record_blocked(session, result)
        fresh = self._legacy.create_fresh_standby_after_merge(prereq.context)
        warnings = [*prereq.warnings, *plan.warnings]
        if bool(remote.get("remote_changed_after_base")):
            warnings.extend(["remote medical DB changed after emergency base; unselected admissions were preserved", "selected admissions use the complete local emergency state"])
        if not fresh.get("ok"):
            warnings.append(f"fresh standby creation warning: {fresh.get('reason')}")
        result = self._success_result(session, prereq, applied, final, remote, lock_status, session_locks, report_path, started_at, started, fresh, backups, plan, warnings)
        return self._finalize_success(session, result)

    def _record_blocked(self, session, result):
        self._mark_merge_failed_if_possible(session, result)
        self.write_merge_report(result)
        return result

    def _success_result(self, session, prereq, applied, final, remote, lock_status, session_locks, report_path, started_at, started, fresh, backups=None, plan=None, warnings=None):
        return self._result(
            session_id=session.emergency_session_id, status="success", error_code="", error=None,
            report_path=report_path, marker_path=prereq.marker_path, dry_run_report_path=prereq.dry_run_report_path,
            remote_backup_path=(backups or {}).get("remote_backup_path", ""), local_backup_path=(backups or {}).get("local_backup_path", ""),
            final_remote_hash=str(final.get("file_hash") or ""), post_quick_check_status=str(final.get("quick_check") or ""),
            post_integrity_check_status=str(final.get("integrity_check") or ""), post_foreign_key_check_status=str(final.get("foreign_key_check") or ""),
            base_last_change_id=int(session.base_last_change_id or 0), local_last_change_id=int(remote.get("local_last_change_id") or 0),
            remote_last_change_id_before=int(remote.get("remote_last_change_id") or 0), remote_last_change_id_after=int(final.get("last_change_id") or 0),
            applied_change_count_estimate=int(applied.get("applied_operations") or 0), changed_tables_summary=(plan.changed_tables_summary if plan else {}),
            conflicts=(plan.conflicts if plan else []), protected_summary=(plan.protected_summary if plan else {}), skipped_tables=(plan.skipped_tables if plan else []),
            locks_acquired=_public_lock_status(lock_status), session_locks_status=session_locks, fresh_standby_status=fresh,
            warnings=warnings or [], started_at=started_at, started=started,
        )

    def _rollback_result(self, session, prereq, plan, final, remote, lock_status, session_locks, report_path, started_at, started, backups, rollback):
        return self._result(
            session_id=session.emergency_session_id, status="rolled_back", error_code="final_validation_failed",
            error=str(final.get("reason") or "final validation failed"),
            blockers=[_blocker("final_validation_failed", str(final.get("reason") or ""))], warnings=[*prereq.warnings, *plan.warnings],
            report_path=report_path, marker_path=prereq.marker_path, dry_run_report_path=prereq.dry_run_report_path,
            remote_backup_path=backups["remote_backup_path"], local_backup_path=backups["local_backup_path"],
            base_last_change_id=int(session.base_last_change_id or 0), local_last_change_id=int(remote.get("local_last_change_id") or 0),
            remote_last_change_id_before=int(remote.get("remote_last_change_id") or 0), applied_change_count_estimate=plan.applied_change_count_estimate,
            changed_tables_summary=plan.changed_tables_summary, conflicts=plan.conflicts, protected_summary=plan.protected_summary, skipped_tables=plan.skipped_tables,
            locks_acquired=_public_lock_status(lock_status), session_locks_status=session_locks, rollback_status=str(rollback.get("status") or ""),
            started_at=started_at, started=started,
        )

    def write_merge_report(self, result: EmergencyRowLevelMergeResult) -> str:
        report = {
            "report_version": ROW_LEVEL_MERGE_REPORT_VERSION,
            "created_at": _now_text(),
            "mode": ROW_LEVEL_MERGE_MODE,
            **result.to_dict(),
        }
        atomic_write_json(result.report_path, report)
        return result.report_path

    def _blocked_result(
        self,
        session: EmergencySessionMetadata,
        prereq: Any,
        code: str,
        reason: str,
        report_path: str,
        started_at: str,
        started: float,
        *,
        blockers: list[dict[str, Any]] | None = None,
        warnings: list[str] | None = None,
        locks_acquired: dict[str, Any] | None = None,
        session_locks_status: dict[str, Any] | None = None,
        remote_last_change_id_before: int = 0,
        plan: RowMergePlan | None = None,
        backups: dict[str, str] | None = None,
    ) -> EmergencyRowLevelMergeResult:
        return self._result(
            session_id=session.emergency_session_id,
            status="blocked",
            error_code=code,
            error=reason,
            blockers=blockers or [_blocker(code, reason)],
            warnings=warnings if warnings is not None else prereq.warnings,
            report_path=report_path,
            marker_path=prereq.marker_path,
            dry_run_report_path=prereq.dry_run_report_path,
            remote_backup_path=(backups or {}).get("remote_backup_path", ""),
            local_backup_path=(backups or {}).get("local_backup_path", ""),
            base_last_change_id=int(session.base_last_change_id or 0),
            local_last_change_id=int((prereq.local_validation.get("local") or {}).get("last_change_id") or 0),
            remote_last_change_id_before=remote_last_change_id_before,
            applied_change_count_estimate=0 if plan is None else plan.applied_change_count_estimate,
            changed_tables_summary={} if plan is None else plan.changed_tables_summary,
            conflicts=[] if plan is None else plan.conflicts,
            protected_summary={} if plan is None else plan.protected_summary,
            skipped_tables=[] if plan is None else plan.skipped_tables,
            locks_acquired=locks_acquired or {},
            session_locks_status=session_locks_status or {},
            started_at=started_at,
            started=started,
        )

    def _result(
        self,
        *,
        session_id: str,
        status: str,
        error_code: str,
        error: str | None,
        report_path: str,
        started_at: str,
        started: float,
        blockers: list[dict[str, Any]] | None = None,
        warnings: list[str] | None = None,
        marker_path: str = "",
        dry_run_report_path: str = "",
        remote_backup_path: str = "",
        local_backup_path: str = "",
        final_remote_hash: str = "",
        post_quick_check_status: str = "",
        post_integrity_check_status: str | None = None,
        post_foreign_key_check_status: str | None = None,
        base_last_change_id: int = 0,
        local_last_change_id: int = 0,
        remote_last_change_id_before: int = 0,
        remote_last_change_id_after: int = 0,
        applied_change_count_estimate: int = 0,
        changed_tables_summary: dict[str, Any] | None = None,
        conflicts: list[dict[str, Any]] | None = None,
        protected_summary: dict[str, Any] | None = None,
        skipped_tables: list[dict[str, str]] | None = None,
        locks_acquired: dict[str, Any] | None = None,
        session_locks_status: dict[str, Any] | None = None,
        rollback_status: str | None = None,
        fresh_standby_status: dict[str, Any] | None = None,
    ) -> EmergencyRowLevelMergeResult:
        finished_at = _now_text()
        return EmergencyRowLevelMergeResult(
            ok=status == "success",
            requires_recovery=(status != "success" and rollback_status != "restored"
                               and bool(getattr(self, "_outcome_uncertain", True))),
            result_status=status,
            error_code=error_code,
            error=error,
            session_id=session_id,
            report_path=report_path,
            user_message=MERGE_SUCCESS_MESSAGE if status == "success" else MERGE_FAILURE_MESSAGE,
            blockers=blockers or [],
            warnings=warnings or [],
            marker_path=marker_path,
            dry_run_report_path=dry_run_report_path,
            remote_backup_path=remote_backup_path,
            local_backup_path=local_backup_path,
            final_remote_hash=final_remote_hash,
            post_quick_check_status=post_quick_check_status,
            post_integrity_check_status=post_integrity_check_status,
            post_foreign_key_check_status=post_foreign_key_check_status,
            base_last_change_id=int(base_last_change_id or 0),
            local_last_change_id=int(local_last_change_id or 0),
            remote_last_change_id_before=int(remote_last_change_id_before or 0),
            remote_last_change_id_after=int(remote_last_change_id_after or 0),
            applied_change_count_estimate=int(applied_change_count_estimate or 0),
            changed_tables_summary=changed_tables_summary or {},
            conflicts=conflicts or [],
            protected_summary=protected_summary or {},
            skipped_tables=skipped_tables or [],
            locks_acquired=locks_acquired or {},
            session_locks_status=session_locks_status or {},
            rollback_status=rollback_status,
            fresh_standby_status=fresh_standby_status or {},
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    def _merge_report_path(self, session_id: str) -> str:
        return os.path.join(
            active_session_dir(self.store.resolve_root(), session_id),
            "logs",
            f"emergency_row_level_merge_{_stamp()}.json",
        )

    def _restore_remote_backup(self, remote_db_path: str, backup_path: str, *, reason: str) -> dict[str, Any]:
        try:
            restore_database(remote_db_path, backup_path)
            validation = validate_medical_db_snapshot(remote_db_path)
            if validation.ok:
                return {"status": "restored", "reason": reason, "remote_last_change_id": int(validation.last_change_id or 0)}
            return {"status": "restore_validation_failed", "reason": validation.reason}
        except Exception as exc:
            return {"status": "rollback_failed", "reason": str(exc)}

    def _finalize_success(
        self,
        session: EmergencySessionMetadata,
        result: EmergencyRowLevelMergeResult,
    ) -> EmergencyRowLevelMergeResult:
        self.write_merge_report(result)
        self._legacy.mark_session_merged(session, result)
        self._legacy._clear_success_markers(session)
        archived_path = self._legacy.archive_emergency_session(session.emergency_session_id)
        active_dir_path = active_session_dir(self.store.resolve_root(), session.emergency_session_id)
        final_result = replace(
            result,
            report_path=_map_session_path(result.report_path, active_dir_path, archived_path),
            dry_run_report_path=_map_session_path(result.dry_run_report_path, active_dir_path, archived_path),
            local_backup_path=_map_session_path(result.local_backup_path, active_dir_path, archived_path),
            archive_path=archived_path,
        )
        self.write_merge_report(final_result)
        return final_result

    def _mark_merge_failed_if_possible(
        self,
        session: EmergencySessionMetadata | None,
        result: EmergencyRowLevelMergeResult,
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
                # Завершение могло сбросить флаг перед ошибкой архивирования.
                # Сохраняем фактическую неопределённость результата операции.
                merge_recovery_required=bool(result.requires_recovery),
                # Пока папка остаётся active, запуск должен находить реальные
                # файлы, даже если mark_session_merged уже подготовил пути архива.
                local_db_path=session.local_db_path,
                base_snapshot_path=session.base_snapshot_path,
                settings_snapshot_path=session.settings_snapshot_path,
                last_dry_run_report_path=session.last_dry_run_report_path,
                local_backup_path=_map_session_path(
                    current.local_backup_path or "", os.path.dirname(current.local_db_path),
                    active_session_dir(self.store.resolve_root(), session.emergency_session_id),
                ) or None,
            )
            self.store.write_active_session(updated)
        except Exception:
            pass


def _append_authoritative_row_operations(
    table,
    pk,
    pk_columns,
    base_row,
    local_row,
    remote_row,
    scope,
    protected,
    operations,
    blockers,
    conflicts,
    table_summary,
    admission_id,
    patient_id,
):
    # The local snapshot is authoritative for every scoped
    # row, including rows changed only on the network and
    # rows that exist only on the network.
    if local_row is None:
        if remote_row is None:
            return
        if _is_protected_common_row(table, remote_row, protected):
            blockers.append(_blocker("protected_common_row_delete", f"delete touches protected {table}{pk}", table=table))
            return
        operations.append(RowOperation(
            table=table, action="delete", pk_columns=pk_columns, pk_values=pk,
            base_row=base_row or {}, remote_row=remote_row,
            admission_id=admission_id, patient_id=patient_id,
        ))
        table_summary["delete"] += 1
        return
    if remote_row is not None and _rows_equal(local_row, remote_row):
        return
    if remote_row is None:
        operations.append(RowOperation(
            table=table, action="insert", pk_columns=pk_columns, pk_values=pk,
            row=local_row, base_row=base_row or {}, admission_id=admission_id, patient_id=patient_id,
        ))
        table_summary["insert"] += 1
        return
    if base_row is None:
        # A base/local pair is the identity proof.  Equal ids
        # of independently created patients/admissions must
        # never be merged as the same person.
        remote_in_selected_scope = _row_matches_remote_scope(table, remote_row, scope)
        # For a child row of the selected card, the remote row
        # is precisely the remote-only state to remove.  Delete
        # it before inserting the local row under its original
        # id.  A patient/admission collision, or an unrelated
        # remote child collision, instead receives a new local
        # id so an independent patient is never overwritten.
        replace_remote_child = remote_in_selected_scope and table not in {"patients", "admissions"}
        force_new_pk = not replace_remote_child and len(pk_columns) == 1 and pk_columns[0] == "id"
        conflict = _conflict(table, "insert_pk_collision", pk, protected=_is_protected_common_row(table, remote_row, protected))
        if replace_remote_child:
            if _is_protected_common_row(table, remote_row, protected):
                blockers.append(_blocker("protected_common_row_delete", f"delete touches protected {table}{pk}", table=table))
                return
            operations.append(RowOperation(
                table=table, action="delete", pk_columns=pk_columns, pk_values=pk,
                base_row={}, remote_row=remote_row, admission_id=admission_id, patient_id=patient_id,
            ))
            table_summary["delete"] += 1
            operations.append(RowOperation(
                table=table, action="insert", pk_columns=pk_columns, pk_values=pk,
                row=local_row, remote_row=remote_row, conflicts=[conflict],
                admission_id=admission_id, patient_id=patient_id,
            ))
            conflicts.append(conflict)
            table_summary["insert"] += 1
            table_summary["conflict_local_wins"] += 1
            return
        if not force_new_pk:
            blockers.append(_blocker("row_pk_collision", f"local insert collides with remote {table}{pk}", table=table))
            return
        operations.append(RowOperation(
            table=table, action="insert", pk_columns=pk_columns, pk_values=pk,
            row=local_row, remote_row=remote_row, insert_new_primary_key=True,
            conflicts=[conflict], admission_id=admission_id, patient_id=patient_id,
        ))
        conflicts.append(conflict)
        table_summary["insert"] += 1
        table_summary["id_remap"] += 1
        table_summary["conflict_local_wins"] += 1
        return
    if _is_protected_common_row(table, remote_row, protected):
        blockers.append(_blocker("protected_common_row_update", f"update touches protected {table}{pk}", table=table))
        return
    op_conflicts: list[dict[str, Any]] = []
    if not _rows_equal(base_row, remote_row):
        conflict = _conflict(table, "authoritative_remote_override", pk)
        op_conflicts.append(conflict)
        conflicts.append(conflict)
        table_summary["conflict_local_wins"] += 1
    operations.append(RowOperation(
        table=table, action="update", pk_columns=pk_columns, pk_values=pk,
        row=local_row, base_row=base_row, remote_row=remote_row,
        conflicts=op_conflicts, admission_id=admission_id, patient_id=patient_id,
    ))
    table_summary["update"] += 1
    return


def _append_three_way_row_operations(
    table,
    pk,
    pk_columns,
    base_row,
    local_row,
    remote_row,
    protected,
    operations,
    blockers,
    conflicts,
    table_summary,
    admission_id,
    patient_id,
):
    if base_row is None and local_row is not None:
        if remote_row is not None and _rows_equal(local_row, remote_row):
            return
        force_new_pk = False
        op_conflicts: list[dict[str, Any]] = []
        if remote_row is not None:
            force_new_pk = len(pk_columns) == 1 and pk_columns[0] == "id"
            conflict = _conflict(table, "insert_pk_collision", pk, protected=_is_protected_common_row(table, remote_row, protected))
            op_conflicts.append(conflict)
            conflicts.append(conflict)
            table_summary["conflict_local_wins"] += 1
            if not force_new_pk:
                if _is_protected_common_row(table, remote_row, protected):
                    blockers.append(
                        _blocker(
                            "protected_common_row_collision",
                            f"local insert collides with protected remote {table}{pk}",
                            table=table,
                        )
                    )
                    return
                blockers.append(
                    _blocker("row_pk_collision", f"local insert collides with remote {table}{pk}", table=table)
                )
                return
        operations.append(
            RowOperation(
                table=table,
                action="insert",
                pk_columns=pk_columns,
                pk_values=pk,
                row=local_row,
                remote_row=remote_row or {},
                insert_new_primary_key=force_new_pk,
                conflicts=op_conflicts,
                admission_id=admission_id,
                patient_id=patient_id,
            )
        )
        table_summary["insert"] += 1
        if force_new_pk:
            table_summary["id_remap"] += 1
        return

    if base_row is not None and local_row is None:
        if remote_row is None:
            return
        if _is_protected_common_row(table, remote_row, protected):
            blockers.append(_blocker("protected_common_row_delete", f"delete touches protected {table}{pk}", table=table))
            return
        op_conflicts = []
        if not _rows_equal(base_row, remote_row):
            conflict = _conflict(table, "delete_remote_changed", pk)
            op_conflicts.append(conflict)
            conflicts.append(conflict)
            table_summary["conflict_local_wins"] += 1
        operations.append(
            RowOperation(
                table=table,
                action="delete",
                pk_columns=pk_columns,
                pk_values=pk,
                base_row=base_row,
                remote_row=remote_row,
                conflicts=op_conflicts,
                admission_id=admission_id,
                patient_id=patient_id,
            )
        )
        table_summary["delete"] += 1
        return

    if base_row is None or local_row is None or _rows_equal(base_row, local_row):
        return
    if remote_row is not None and _rows_equal(local_row, remote_row):
        return
    if remote_row is not None and _is_protected_common_row(table, remote_row, protected):
        blockers.append(_blocker("protected_common_row_update", f"update touches protected {table}{pk}", table=table))
        return
    op_conflicts = []
    if remote_row is not None and not _rows_equal(base_row, remote_row):
        conflict = _conflict(table, "update_remote_changed", pk)
        op_conflicts.append(conflict)
        conflicts.append(conflict)
        table_summary["conflict_local_wins"] += 1
    operations.append(
        RowOperation(
            table=table,
            action="update",
            pk_columns=pk_columns,
            pk_values=pk,
            row=local_row,
            base_row=base_row,
            remote_row=remote_row or {},
            conflicts=op_conflicts,
            admission_id=admission_id,
            patient_id=patient_id,
        )
    )
    table_summary["update"] += 1


def build_row_merge_plan(
    *,
    base_db_path: str,
    local_db_path: str,
    remote_db_path: str,
    selected_admission_ids: list[int] | None = None,
    authoritative: bool = False,
) -> RowMergePlan:
    """Build a reproducible row plan.

    The historical three-way behaviour remains the default.  Authoritative
    mode is intentionally opt-in: within a reviewed admission scope the local
    snapshot is the complete desired state, not merely a list of local edits.
    """
    base_conn = _connect(base_db_path, readonly=True)
    local_conn = _connect(local_db_path, readonly=True)
    remote_conn = _connect(remote_db_path, readonly=True)
    try:
        base_tables = _table_names(base_conn)
        local_tables = _table_names(local_conn)
        remote_tables = _table_names(remote_conn)
        protected = _load_remote_protected_scope(remote_conn)
        operations: list[RowOperation] = []
        blockers: list[dict[str, Any]] = []
        warnings: list[str] = []
        conflicts: list[dict[str, Any]] = []
        skipped_tables: list[dict[str, str]] = []
        summary: dict[str, dict[str, int]] = {}

        scope = _AuthoritativeScope.empty()
        if authoritative:
            scope, scope_blockers = _build_authoritative_scope(
                base_conn, local_conn, remote_conn, selected_admission_ids
            )
            blockers.extend(scope_blockers)
            blockers.extend(_authoritative_unknown_table_blockers(base_conn, local_conn, remote_conn))
            for issue in _foreign_key_issues(local_conn):
                blockers.append(_blocker("local_foreign_key_violation", "В локальной аварийной базе нарушена внешняя ссылка.", issue=issue))
            for issue in _foreign_key_issues(remote_conn):
                blockers.append(_blocker("remote_foreign_key_violation", "В сетевой базе нарушена внешняя ссылка.", issue=issue))

        for table in sorted((local_tables | base_tables) - set(REMCARD_MERGE_TABLE_ORDER) - PROTECTED_OPBLOCK_TABLES - NON_REPLAY_TABLES):
            skipped_tables.append({"table": table, "reason": "not_in_remcard_row_merge_allowlist"})

        for table in REMCARD_MERGE_TABLE_ORDER:
            if table not in local_tables and table not in base_tables:
                continue
            if table not in remote_tables:
                blockers.append(_blocker("remote_schema_missing_table", f"remote DB does not contain table {table}"))
                continue
            if table not in local_tables:
                blockers.append(_blocker("local_schema_missing_table", f"local emergency DB does not contain table {table}"))
                continue
            if table not in base_tables:
                blockers.append(_blocker("base_schema_missing_table", f"base snapshot does not contain table {table}"))
                continue

            pk_columns = _primary_key_columns(local_conn, table)
            if not pk_columns:
                skipped_tables.append({"table": table, "reason": "no_primary_key"})
                continue
            column_check = _compatible_columns(base_conn, local_conn, remote_conn, table)
            if not column_check.get("ok"):
                blockers.append(_blocker("schema_column_mismatch", str(column_check.get("reason") or table)))
                continue

            base_rows = _rows_by_pk(base_conn, table, pk_columns)
            local_rows = _rows_by_pk(local_conn, table, pk_columns)
            remote_rows = _rows_by_pk(remote_conn, table, pk_columns)
            table_summary = {"insert": 0, "update": 0, "delete": 0, "conflict_local_wins": 0, "id_remap": 0}

            all_keys = set(base_rows) | set(local_rows)
            if authoritative:
                all_keys |= set(remote_rows)
            for pk in sorted(all_keys, key=_sort_key):
                base_row = base_rows.get(pk)
                local_row = local_rows.get(pk)
                remote_row = remote_rows.get(pk)
                if (authoritative and local_row is not None and remote_row is not None
                        and not _row_is_in_authoritative_scope(table, None, local_row, None, scope)
                        and _row_is_in_authoritative_scope(table, None, None, remote_row, scope)
                        and not _rows_equal(local_row, remote_row)):
                    blockers.append(_blocker("selected_scope_identity_collision", "Одинаковый номер записи относится к выбранной и невыбранной госпитализации. Выберите связанные карты вместе.", table=table))
                    continue
                if authoritative and not _row_is_in_authoritative_scope(
                    table, base_row, local_row, remote_row, scope
                ):
                    continue
                admission_id, patient_id = _operation_identity(
                    table, base_row, local_row, remote_row, scope
                )
                if authoritative:
                    _append_authoritative_row_operations(
                        table, pk, pk_columns, base_row, local_row, remote_row,
                        scope, protected, operations, blockers, conflicts,
                        table_summary, admission_id, patient_id,
                    )
                    continue
                _append_three_way_row_operations(
                    table, pk, pk_columns, base_row, local_row, remote_row,
                    protected, operations, blockers, conflicts, table_summary,
                    admission_id, patient_id,
                )

            if any(table_summary.values()):
                summary[table] = table_summary

        if conflicts:
            warnings.append(f"local-wins conflicts detected: {len(conflicts)}")
        if authoritative:
            blockers.extend(_authoritative_bed_operation_blockers(operations, scope))
        return RowMergePlan(
            operations=operations,
            blockers=blockers,
            warnings=warnings,
            changed_tables_summary=summary,
            conflicts=conflicts,
            protected_summary=protected.to_report(),
            skipped_tables=skipped_tables,
        )
    finally:
        base_conn.close()
        local_conn.close()
        remote_conn.close()


@dataclass(frozen=True)
class _AuthoritativeScope:
    selected_admission_ids: frozenset[int] = frozenset()
    remote_admission_ids: frozenset[int] = frozenset()
    patient_ids: frozenset[int] = frozenset()
    order_ids: frozenset[int] = frozenset()
    remote_order_ids: frozenset[int] = frozenset()
    order_admission_ids: dict[int, int] = field(default_factory=dict)
    procedure_ids: frozenset[int] = frozenset()
    remote_procedure_ids: frozenset[int] = frozenset()
    procedure_admission_ids: dict[int, int] = field(default_factory=dict)
    template_ids: frozenset[int] = frozenset()
    remote_template_ids: frozenset[int] = frozenset()
    template_admission_ids: dict[int, int] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "_AuthoritativeScope":
        return cls()


def _build_authoritative_scope(
    base_conn: sqlite3.Connection,
    local_conn: sqlite3.Connection,
    remote_conn: sqlite3.Connection,
    selected_admission_ids: list[int] | None,
) -> tuple[_AuthoritativeScope, list[dict[str, Any]]]:
    admission_rows, admission_error = _authoritative_admission_rows(base_conn, local_conn, remote_conn)
    if admission_error:
        return _AuthoritativeScope.empty(), [admission_error]
    base, local, remote = admission_rows
    selected, blockers = _resolve_authoritative_selection(base_conn, local_conn, remote_conn, base, local, selected_admission_ids)
    patient_ids = _selected_patient_ids(selected, base, local)
    blockers.extend(_shared_scope_blockers(selected, patient_ids, base, local, base_conn, local_conn, remote_conn))
    remote_ids = _matching_remote_admission_ids(selected, base, local, remote)
    order_ids, remote_order_ids, order_map = _scoped_fk_ids(local_conn, remote_conn, "orders", selected, remote_ids)
    procedure_ids, remote_procedure_ids, procedure_map = _scoped_fk_ids(local_conn, remote_conn, "procedures", selected, remote_ids)
    template_ids, remote_template_ids, template_map, template_blockers = _scoped_template_ids(base_conn, local_conn, remote_conn, selected, remote_ids)
    blockers.extend(template_blockers)
    return _AuthoritativeScope(
        frozenset(selected), frozenset(remote_ids), frozenset(patient_ids),
        frozenset(order_ids), frozenset(remote_order_ids), order_map,
        frozenset(procedure_ids), frozenset(remote_procedure_ids), procedure_map,
        frozenset(template_ids), frozenset(remote_template_ids), template_map,
    ), blockers


def _authoritative_admission_rows(base_conn, local_conn, remote_conn):
    if "admissions" not in _table_names(local_conn) or "admissions" not in _table_names(base_conn):
        return None, _blocker("authoritative_admissions_missing", "Для авторитетной сверки нужна таблица поступлений.")
    if _primary_key_columns(local_conn, "admissions") != ("id",):
        return None, _blocker("authoritative_admissions_key", "Для авторитетной сверки у поступлений требуется первичный ключ id.")
    base = _rows_by_pk(base_conn, "admissions", ("id",))
    local = _rows_by_pk(local_conn, "admissions", ("id",))
    remote = _rows_by_pk(remote_conn, "admissions", ("id",)) if "admissions" in _table_names(remote_conn) else {}
    return (base, local, remote), None


def _resolve_authoritative_selection(base_conn, local_conn, remote_conn, base, local, requested):
    operblock_ids = _load_remote_protected_scope(remote_conn).admission_ids
    known = {int(key[0]) for key, row in local.items() if _is_rao_admission(row)
             and (key not in base or str(row.get("unit_scope") or "").lower() == "rao"
                  or int(key[0]) not in operblock_ids)}
    if requested is not None:
        parsed = {_safe_int(value) for value in requested}
        blockers = [_blocker("selected_admission_invalid", "Список выбранных поступлений содержит некорректный идентификатор.")] if None in parsed else []
        selected = {int(value) for value in parsed if value is not None}
        unknown = sorted(selected - known)
        if unknown:
            blockers.append(_blocker("selected_admission_unknown", f"Выбранное поступление отсутствует в локальной аварийной базе: {unknown}.", admission_ids=unknown))
        return selected, blockers
    # A preview must expose remote-only changes on an otherwise untouched
    # local card; the reviewer, rather than a local edit, chooses its scope.
    return known, []


def _is_rao_admission(row):
    # Origin is not ownership: an RAO patient can arrive from an operating room.
    return str(row.get("unit_scope") or "").lower() != "operblock" and str(row.get("admission_type") or "").lower() != "operblock"


def _changed_child_admissions(base_conn, local_conn, local_admissions):
    selected: set[int] = set()
    for table in REMCARD_MERGE_TABLE_ORDER:
        if table not in _table_names(base_conn) or table not in _table_names(local_conn):
            continue
        pk = _primary_key_columns(local_conn, table)
        if not pk:
            continue
        base_rows, local_rows = _rows_by_pk(base_conn, table, pk), _rows_by_pk(local_conn, table, pk)
        for key in set(base_rows) | set(local_rows):
            if not _rows_equal(base_rows.get(key, {}), local_rows.get(key, {})):
                selected.update(_row_admission_candidates(table, base_rows.get(key), local_rows.get(key), local_admissions))
    return selected


def _row_admission_candidates(table, base_row, local_row, local_admissions):
    rows = (base_row, local_row)
    direct = {_safe_int(row.get("admission_id")) for row in rows if row}
    if table != "patients":
        return {int(value) for value in direct if value is not None}
    patient_ids = {_safe_int(row.get("id")) for row in rows if row}
    return {int(key[0]) for key, row in local_admissions.items() if _safe_int(row.get("patient_id")) in patient_ids}


def _selected_patient_ids(selected, base, local):
    patient_ids: set[int] = set()
    for admission_id in selected:
        for rows in (base, local):
            if row := rows.get((admission_id,)):
                _add_int(patient_ids, row.get("patient_id"))
    return patient_ids


def _authoritative_bed_operation_blockers(operations, scope):
    blockers = []
    for operation in operations:
        if operation.table != "beds":
            continue
        before = _safe_int(operation.remote_row.get("current_admission_id"))
        after = _safe_int(operation.row.get("current_admission_id"))
        if ((before is not None and before not in scope.remote_admission_ids)
                or (after is not None and after not in scope.selected_admission_ids)):
            blockers.append(_blocker("authoritative_bed_conflict", "Изменение койки затрагивает невыбранного пациента. Выберите связанные госпитализации или проверьте размещение.", bed_id=list(operation.pk_values)))
    return blockers


def _shared_scope_blockers(selected, patient_ids, base, local, base_conn, local_conn, remote_conn):
    active_selected = {key for key, row in local.items() if int(key[0]) in selected and _is_active_admission(row)}
    selected_beds = {_safe_int(local[key].get("bed_number")) for key in active_selected}
    changed_patients = _changed_selected_patient_ids(patient_ids, local_conn, remote_conn)
    blockers = []
    for key, row in local.items():
        admission_id = int(key[0])
        if admission_id not in selected and _safe_int(row.get("patient_id")) in changed_patients:
            blockers.append(_blocker("selected_scope_shared_patient", "Нельзя выбрать только часть карт: выбранная и невыбранная карты ссылаются на одного пациента.", admission_id=admission_id))
        if admission_id in selected or not _is_active_admission(row):
            continue
        if _safe_int(row.get("bed_number")) in selected_beds - {None}:
            blockers.append(_blocker("selected_scope_shared_bed", "Нельзя выбрать только часть карт: выбранная и невыбранная карты используют одну койку.", admission_id=admission_id))
    remote_rows = _rows_by_pk(remote_conn, "admissions", ("id",)) if "admissions" in _table_names(remote_conn) else {}
    remote_selected = _matching_remote_admission_ids(selected, base, local, remote_rows)
    base_patients = _rows_by_pk(base_conn, "patients", ("id",)) if "patients" in _table_names(base_conn) else {}
    for row in remote_rows.values():
        admission_id, patient_id = _safe_int(row.get("id")), _safe_int(row.get("patient_id"))
        if (admission_id not in remote_selected and patient_id in changed_patients
                and (patient_id,) in base_patients):
            blockers.append(_blocker("selected_scope_shared_patient", "Общие сведения пациента используются невыбранной госпитализацией основной базы.", admission_id=admission_id))
    return blockers


def _is_active_admission(row):
    if "is_active" in row:
        return bool(row.get("is_active"))
    return not any(row.get(column) for column in ("transfer_datetime", "discharge_datetime", "death_datetime"))


def _changed_selected_patient_ids(patient_ids, local_conn, remote_conn):
    # Patient rows are shared identity data.  A repeated historical admission
    # is harmless until the selected scope would actually change that row.
    if "patients" not in _table_names(local_conn) or "patients" not in _table_names(remote_conn):
        return set()
    pk = _primary_key_columns(local_conn, "patients")
    if pk != ("id",):
        return set()
    local_rows = _rows_by_pk(local_conn, "patients", pk)
    remote_rows = _rows_by_pk(remote_conn, "patients", pk)
    return {patient_id for patient_id in patient_ids if not _rows_equal(local_rows.get((patient_id,), {}), remote_rows.get((patient_id,), {}))}


def _matching_remote_admission_ids(selected, base, local, remote):
    return {admission_id for admission_id in selected if (admission_id,) in base and (admission_id,) in local and (admission_id,) in remote and _safe_int(base[(admission_id,)].get("patient_id")) == _safe_int(local[(admission_id,)].get("patient_id"))}


def _scoped_fk_ids(local_conn, remote_conn, table, selected, remote_selected):
    local_ids, remote_ids, mapping = set(), set(), {}
    for conn, admissions, target, overwrite in ((local_conn, selected, local_ids, True), (remote_conn, remote_selected, remote_ids, False)):
        if table not in _table_names(conn) or _primary_key_columns(conn, table) != ("id",):
            continue
        for key, row in _rows_by_pk(conn, table, ("id",)).items():
            entity_id, admission_id = _safe_int(key[0]), _safe_int(row.get("admission_id"))
            if entity_id is not None and admission_id in admissions:
                target.add(entity_id)
                if overwrite or entity_id not in mapping:
                    mapping[entity_id] = int(admission_id)
    return local_ids, remote_ids, mapping


def _scoped_template_ids(base_conn, local_conn, remote_conn, selected, remote_selected):
    local_ids, remote_ids, mapping, blockers = set(), set(), {}, []
    for conn, admissions, target, remote in ((local_conn, selected, local_ids, False), (remote_conn, remote_selected, remote_ids, True)):
        for table in ("diet_plan", "diet_plan_versions"):
            if table not in _table_names(conn) or "template_id" not in _columns(conn, table):
                continue
            for row in conn.execute(f"SELECT admission_id, template_id FROM {_quote_ident(table)}"):
                admission_id, template_id = _safe_int(row[0]), _safe_int(row[1])
                if template_id is None:
                    continue
                if admission_id in admissions:
                    target.add(template_id); mapping.setdefault(template_id, int(admission_id))
    all_template_ids = local_ids | remote_ids
    blockers.extend(_shared_template_blockers(base_conn, local_conn, remote_conn, selected, remote_selected, all_template_ids))
    return local_ids, remote_ids, mapping, blockers


def _shared_template_blockers(base_conn, local_conn, remote_conn, selected, remote_selected, template_ids):
    shared = set()
    for conn, admissions in ((local_conn, selected), (remote_conn, remote_selected)):
        for table in ("diet_plan", "diet_plan_versions"):
            if table not in _table_names(conn) or "template_id" not in _columns(conn, table):
                continue
            for row in conn.execute(f"SELECT admission_id, template_id FROM {_quote_ident(table)}"):
                admission_id, template_id = _safe_int(row[0]), _safe_int(row[1])
                if admission_id not in admissions and template_id in template_ids:
                    shared.add(template_id)
    return [_blocker("selected_scope_shared_template", "Нельзя выбрать только часть карт: выбранная и невыбранная карты используют один шаблон питания.", template_id=template_id) for template_id in sorted(shared) if _template_needs_authoritative_write(base_conn, local_conn, remote_conn, template_id)]


def _template_needs_authoritative_write(base_conn, local_conn, remote_conn, template_id):
    if any("diet_templates" not in _table_names(conn) for conn in (base_conn, local_conn, remote_conn)):
        return False
    rows = []
    for conn in (base_conn, local_conn, remote_conn):
        pk = _primary_key_columns(conn, "diet_templates")
        if pk != ("id",):
            return False
        rows.append(_rows_by_pk(conn, "diet_templates", pk).get((template_id,), {}))
    return not _rows_equal(rows[1], rows[2])


def _authoritative_unknown_table_blockers(base_conn: sqlite3.Connection, local_conn: sqlite3.Connection, remote_conn: sqlite3.Connection) -> list[dict[str, Any]]:
    known = set(REMCARD_MERGE_TABLE_ORDER) | PROTECTED_OPBLOCK_TABLES | NON_REPLAY_TABLES | {"emergency_merge_receipts"}
    blockers: list[dict[str, Any]] = []
    for table in sorted((_table_names(base_conn) | _table_names(local_conn) | _table_names(remote_conn)) - known):
        signatures = []
        for conn in (base_conn, local_conn, remote_conn):
            signatures.append(_table_signature(conn, table) if table in _table_names(conn) else "<missing>")
        if len(set(signatures)) > 1:
            blockers.append(_blocker("unknown_clinical_table_changed", f"Неизвестная клиническая таблица {table} изменилась; авторитетная сверка остановлена.", table=table))
    return blockers


def _table_signature(conn: sqlite3.Connection, table: str) -> str:
    columns = _columns(conn, table)
    rows = [dict(row) for row in conn.execute(f"SELECT * FROM {_quote_ident(table)}")]
    return _row_fingerprint({"columns": columns, "rows": sorted((_row_fingerprint(row) for row in rows))})


def _row_is_in_authoritative_scope(table: str, base_row: dict[str, Any] | None, local_row: dict[str, Any] | None, remote_row: dict[str, Any] | None, scope: _AuthoritativeScope) -> bool:
    # Use local/base identity for the desired state.  Remote-only rows are in
    # scope solely through a proven pre-existing selected admission.
    for row, is_remote in ((local_row, False), (base_row, False), (remote_row, True)):
        if not row:
            continue
        admissions = scope.remote_admission_ids if is_remote else scope.selected_admission_ids
        direct_admission = _safe_int(row.get("id")) if table == "admissions" else _safe_int(row.get("admission_id"))
        if direct_admission is not None:
            return direct_admission in admissions
        current_admission = _safe_int(row.get("current_admission_id"))
        if current_admission is not None:
            return current_admission in admissions
        order_id = _safe_int(row.get("order_id"))
        if order_id in (scope.remote_order_ids if is_remote else scope.order_ids):
            return True
        procedure_id = _safe_int(row.get("procedure_id"))
        if procedure_id in (scope.remote_procedure_ids if is_remote else scope.procedure_ids):
            return True
        if table == "diet_templates" and _safe_int(row.get("id")) in (scope.remote_template_ids if is_remote else scope.template_ids):
            return True
        if table == "patients" and _safe_int(row.get("id")) in scope.patient_ids:
            return True
        if _safe_int(row.get("patient_id")) in scope.patient_ids:
            return True
    return False


def _row_matches_remote_scope(table: str, row: dict[str, Any], scope: _AuthoritativeScope) -> bool:
    if table == "admissions" and _safe_int(row.get("id")) in scope.remote_admission_ids:
        return True
    if _safe_int(row.get("admission_id")) in scope.remote_admission_ids:
        return True
    if _safe_int(row.get("current_admission_id")) in scope.remote_admission_ids:
        return True
    if _safe_int(row.get("order_id")) in scope.remote_order_ids:
        return True
    if _safe_int(row.get("procedure_id")) in scope.remote_procedure_ids:
        return True
    return table == "patients" and _safe_int(row.get("id")) in scope.patient_ids


def _operation_identity(table: str, base_row: dict[str, Any] | None, local_row: dict[str, Any] | None, remote_row: dict[str, Any] | None, scope: _AuthoritativeScope) -> tuple[int | None, int | None]:
    rows = (local_row, base_row, remote_row)
    admission_id = next((_safe_int(row.get("id")) for row in rows if row and table == "admissions" and _safe_int(row.get("id")) is not None), None)
    if admission_id is None:
        admission_id = next((_safe_int(row.get("admission_id")) for row in rows if row and _safe_int(row.get("admission_id")) is not None), None)
    if admission_id is None:
        admission_id = next((_safe_int(row.get("current_admission_id")) for row in rows if row and _safe_int(row.get("current_admission_id")) is not None), None)
    if admission_id is None:
        for row in rows:
            if row and _safe_int(row.get("order_id")) in scope.order_admission_ids:
                admission_id = scope.order_admission_ids[_safe_int(row.get("order_id"))]
                break
    if admission_id is None:
        for row in rows:
            if row and _safe_int(row.get("procedure_id")) in scope.procedure_admission_ids:
                admission_id = scope.procedure_admission_ids[_safe_int(row.get("procedure_id"))]
                break
    if admission_id is None and table == "diet_templates":
        for row in rows:
            if row and _safe_int(row.get("id")) in scope.template_admission_ids:
                admission_id = scope.template_admission_ids[_safe_int(row.get("id"))]
                break
    patient_id = next((_safe_int(row.get("id")) for row in rows if row and table == "patients" and _safe_int(row.get("id")) is not None), None)
    if patient_id is None:
        patient_id = next((_safe_int(row.get("patient_id")) for row in rows if row and _safe_int(row.get("patient_id")) is not None), None)
    if (scope.selected_admission_ids or scope.remote_admission_ids) and admission_id not in scope.selected_admission_ids and admission_id not in scope.remote_admission_ids:
        admission_id = None
    if scope.patient_ids and patient_id not in scope.patient_ids:
        patient_id = None
    return admission_id, patient_id


def plan_digest(plan: RowMergePlan) -> str:
    """Digest the reviewed plan, including decisions which influence apply."""
    payload = {
        "operations": [asdict(item) for item in plan.operations],
        "blockers": plan.blockers,
        "warnings": plan.warnings,
        "changed_tables_summary": plan.changed_tables_summary,
        "conflicts": plan.conflicts,
        "protected_summary": plan.protected_summary,
        "skipped_tables": plan.skipped_tables,
    }
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=_jsonable)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def apply_row_merge_plan(remote_db_path: str, plan: RowMergePlan, *, session_id: str | None = None, approved_plan_digest: str | None = None) -> dict[str, Any]:
    conn = _connect(remote_db_path, readonly=False)
    id_map: dict[tuple[str, Any], Any] = {}
    applied = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if session_id and approved_plan_digest:
                _ensure_merge_receipts_table(conn)
                if _has_merge_receipt_conn(conn, session_id, approved_plan_digest):
                    conn.execute("COMMIT")
                    return {"applied_operations": 0, "id_remap_count": 0, "receipt_reused": True}
            for op in reversed([item for item in plan.operations if item.action == "delete"]):
                _apply_delete(conn, op)
                applied += 1
            for op in [item for item in plan.operations if item.action in {"insert", "update"}]:
                if op.action == "insert":
                    new_id = _apply_insert(conn, op, id_map)
                    if op.insert_new_primary_key:
                        id_map[(op.table, op.pk_values[0])] = new_id
                else:
                    _apply_update_or_restore(conn, op, id_map)
                applied += 1
            fk_issues = _foreign_key_issues(conn)
            if fk_issues:
                raise sqlite3.IntegrityError(f"foreign_key_check failed: {fk_issues[:5]}")
            if session_id and approved_plan_digest:
                conn.execute(
                    "INSERT INTO emergency_merge_receipts(session_id, plan_digest, committed_at) VALUES (?, ?, ?)",
                    (session_id, approved_plan_digest, _now_text()),
                )
            conn.execute("COMMIT")
            return {"applied_operations": applied, "id_remap_count": len(id_map)}
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def has_merge_receipt(remote_db_path: str, session_id: str, approved_plan_digest: str) -> bool:
    conn = _connect(remote_db_path, readonly=True)
    try:
        if "emergency_merge_receipts" not in _table_names(conn):
            return False
        return _has_merge_receipt_conn(conn, session_id, approved_plan_digest)
    finally:
        conn.close()


def _ensure_merge_receipts_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS emergency_merge_receipts ("
        "session_id TEXT NOT NULL, plan_digest TEXT NOT NULL, committed_at TEXT NOT NULL, "
        "PRIMARY KEY(session_id, plan_digest))"
    )


def _has_merge_receipt_conn(conn: sqlite3.Connection, session_id: str, approved_plan_digest: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM emergency_merge_receipts WHERE session_id = ? AND plan_digest = ?",
        (session_id, approved_plan_digest),
    ).fetchone() is not None


@dataclass(frozen=True)
class _ProtectedScope:
    patient_ids: frozenset[int] = frozenset()
    admission_ids: frozenset[int] = frozenset()
    order_ids: frozenset[int] = frozenset()

    def to_report(self) -> dict[str, int]:
        return {
            "opblock_patient_ids": len(self.patient_ids),
            "opblock_admission_ids": len(self.admission_ids),
            "opblock_order_ids": len(self.order_ids),
        }


def _load_remote_protected_scope(conn: sqlite3.Connection) -> _ProtectedScope:
    tables = _table_names(conn)
    if "operation_cases" not in tables:
        return _ProtectedScope()
    patient_ids: set[int] = set()
    admission_ids: set[int] = set()
    columns = _columns(conn, "operation_cases")
    select_cols = [name for name in ("patient_id", "admission_id", "future_rao_admission_id") if name in columns]
    if select_cols:
        for row in conn.execute(f"SELECT {', '.join(select_cols)} FROM operation_cases"):
            data = dict(row)
            _add_int(patient_ids, data.get("patient_id"))
            _add_int(admission_ids, data.get("admission_id"))
            _add_int(admission_ids, data.get("future_rao_admission_id"))
    order_ids: set[int] = set()
    if "orders" in tables and admission_ids:
        placeholders = ",".join("?" for _ in admission_ids)
        for row in conn.execute(f"SELECT id FROM orders WHERE admission_id IN ({placeholders})", tuple(admission_ids)):
            _add_int(order_ids, row[0])
    return _ProtectedScope(frozenset(patient_ids), frozenset(admission_ids), frozenset(order_ids))


def _is_protected_common_row(table: str, row: dict[str, Any], protected: _ProtectedScope) -> bool:
    if not protected.patient_ids and not protected.admission_ids and not protected.order_ids:
        return False
    if table == "patients":
        return _safe_int(row.get("id")) in protected.patient_ids
    if table == "admissions":
        return _safe_int(row.get("id")) in protected.admission_ids
    if table == "orders":
        return _safe_int(row.get("id")) in protected.order_ids or _safe_int(row.get("admission_id")) in protected.admission_ids
    if table == "administrations":
        return _safe_int(row.get("order_id")) in protected.order_ids
    if "admission_id" in row and _safe_int(row.get("admission_id")) in protected.admission_ids:
        return True
    if "patient_id" in row and _safe_int(row.get("patient_id")) in protected.patient_ids:
        return True
    return False


def _apply_delete(conn: sqlite3.Connection, op: RowOperation) -> None:
    where, params = _where_pk(op.pk_columns, op.pk_values)
    conn.execute(f"DELETE FROM {_quote_ident(op.table)} WHERE {where}", params)


def _apply_insert(conn: sqlite3.Connection, op: RowOperation, id_map: dict[tuple[str, Any], Any]) -> Any:
    row = _remapped_row(op.row, id_map)
    if op.insert_new_primary_key and op.pk_columns == ("id",):
        row = {key: value for key, value in row.items() if key != "id"}
    columns = list(row.keys())
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO {_quote_ident(op.table)} ({_column_list(columns)}) VALUES ({placeholders})"
    cursor = conn.execute(sql, tuple(row[column] for column in columns))
    return cursor.lastrowid if op.insert_new_primary_key else op.pk_values[0] if op.pk_values else cursor.lastrowid


def _apply_update_or_restore(conn: sqlite3.Connection, op: RowOperation, id_map: dict[tuple[str, Any], Any]) -> None:
    existing = conn.execute(
        f"SELECT 1 FROM {_quote_ident(op.table)} WHERE {_where_pk(op.pk_columns, op.pk_values)[0]}",
        _where_pk(op.pk_columns, op.pk_values)[1],
    ).fetchone()
    if existing is None:
        _apply_insert(conn, op, id_map)
        return
    row = _remapped_row(op.row, id_map)
    non_pk_columns = [column for column in row if column not in op.pk_columns]
    if not non_pk_columns:
        return
    assignments = ", ".join(f"{_quote_ident(column)} = ?" for column in non_pk_columns)
    where, where_params = _where_pk(op.pk_columns, op.pk_values)
    params = tuple(row[column] for column in non_pk_columns) + where_params
    conn.execute(f"UPDATE {_quote_ident(op.table)} SET {assignments} WHERE {where}", params)


def _remapped_row(row: dict[str, Any], id_map: dict[tuple[str, Any], Any]) -> dict[str, Any]:
    result = dict(row)
    for column, referenced_table in FK_REMAP_COLUMNS.items():
        if column not in result or result[column] is None:
            continue
        mapped = id_map.get((referenced_table, result[column]))
        if mapped is not None:
            result[column] = mapped
    return result


def _connect(path: str, *, readonly: bool) -> sqlite3.Connection:
    if readonly:
        uri = build_sqlite_file_uri(path, mode="ro")
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False, isolation_level=None, timeout=10.0)
    else:
        conn = sqlite3.connect(os.path.abspath(path), check_same_thread=False, isolation_level=None, timeout=10.0)
    configure_connection(conn, readonly=readonly, profile="network")
    return conn


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        if row[0]
    }


def _columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(str(row["name"]) for row in conn.execute(f"PRAGMA table_info({_quote_ident(table)})"))


def _primary_key_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    rows = [dict(row) for row in conn.execute(f"PRAGMA table_info({_quote_ident(table)})")]
    pk_rows = sorted((row for row in rows if int(row.get("pk") or 0) > 0), key=lambda row: int(row["pk"]))
    return tuple(str(row["name"]) for row in pk_rows)


def _compatible_columns(
    base_conn: sqlite3.Connection,
    local_conn: sqlite3.Connection,
    remote_conn: sqlite3.Connection,
    table: str,
) -> dict[str, Any]:
    base_columns = set(_columns(base_conn, table))
    local_columns = set(_columns(local_conn, table))
    remote_columns = set(_columns(remote_conn, table))
    if base_columns != local_columns:
        return {"ok": False, "reason": f"base/local columns differ for {table}"}
    missing_remote = local_columns - remote_columns
    if missing_remote:
        return {"ok": False, "reason": f"remote {table} missing columns: {sorted(missing_remote)}"}
    return {"ok": True}


def _rows_by_pk(conn: sqlite3.Connection, table: str, pk_columns: tuple[str, ...]) -> dict[tuple[Any, ...], dict[str, Any]]:
    rows: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in conn.execute(f"SELECT * FROM {_quote_ident(table)}"):
        data = dict(row)
        rows[tuple(data[column] for column in pk_columns)] = data
    return rows


def _rows_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return _row_fingerprint(left) == _row_fingerprint(right)


def _row_fingerprint(row: dict[str, Any]) -> str:
    return json.dumps({key: _jsonable(value) for key, value in sorted(row.items())}, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"__bytes__": value.hex()}
    return value


def _foreign_key_issues(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute("PRAGMA foreign_key_check")]


def _where_pk(pk_columns: tuple[str, ...], pk_values: tuple[Any, ...]) -> tuple[str, tuple[Any, ...]]:
    return " AND ".join(f"{_quote_ident(column)} = ?" for column in pk_columns), tuple(pk_values)


def _column_list(columns: list[str]) -> str:
    return ", ".join(_quote_ident(column) for column in columns)


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _conflict(table: str, kind: str, pk: tuple[Any, ...], *, protected: bool = False) -> dict[str, Any]:
    return {"table": table, "kind": kind, "pk": list(pk), "resolution": "local_wins", "protected_remote_row": bool(protected)}


def _blocker(code: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "reason": str(reason or ""), **extra}


def _first_blocker_code(blockers: list[dict[str, Any]], fallback: str) -> str:
    return str((blockers[0] or {}).get("code") or fallback) if blockers else fallback


def _first_blocker_reason(blockers: list[dict[str, Any]]) -> str:
    return str((blockers[0] or {}).get("reason") or "") if blockers else ""


def _public_lock_status(status: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in status.items() if key != "_locks"}


def _safe_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


def _add_int(target: set[int], value: Any) -> None:
    parsed = _safe_int(value)
    if parsed is not None:
        target.add(parsed)


def _sort_key(pk: tuple[Any, ...]) -> tuple[str, ...]:
    return tuple(str(item) for item in pk)


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def _map_session_path(path: str, active_dir_path: str, archive_dir_path: str) -> str:
    if not path:
        return ""
    try:
        rel = os.path.relpath(path, active_dir_path)
        if rel.startswith(".."):
            return path
        return os.path.join(archive_dir_path, rel)
    except Exception:
        return path
