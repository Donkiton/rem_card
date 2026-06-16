from __future__ import annotations

import json
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
    ) -> EmergencyRowLevelMergeResult:
        started_at = _now_text()
        started = time.perf_counter()
        report_path = self._merge_report_path(session_id)
        locks: list[Any] = []
        session: EmergencySessionMetadata | None = None
        try:
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

            lock_status = self._legacy.acquire_merge_locks(prereq.context)
            if not lock_status.get("ok"):
                result = self._blocked_result(
                    session,
                    prereq,
                    "locks_unavailable",
                    str(lock_status.get("reason") or "merge locks unavailable"),
                    report_path,
                    started_at,
                    started,
                    locks_acquired=_public_lock_status(lock_status),
                )
                self._mark_merge_failed_if_possible(session, result)
                self.write_merge_report(result)
                return result
            locks = list(lock_status.get("_locks") or [])

            session_locks = self._legacy._recheck_session_locks(prereq.context)
            if not session_locks.get("ok"):
                result = self._blocked_result(
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

            remote_before = self._legacy._validate_remote_unchanged(session, prereq.context)
            if not remote_before.get("ok"):
                result = self._blocked_result(
                    session,
                    prereq,
                    str(remote_before.get("code") or "remote_invalid"),
                    str(remote_before.get("reason") or "remote validation failed"),
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

            backups = self._legacy.create_pre_merge_backups(session, prereq.context)
            plan = build_row_merge_plan(
                base_db_path=session.base_snapshot_path,
                local_db_path=session.local_db_path,
                remote_db_path=prereq.context.medical_db_path,
            )
            if not plan.ok:
                result = self._blocked_result(
                    session,
                    prereq,
                    "row_merge_plan_blocked",
                    _first_blocker_reason(plan.blockers),
                    report_path,
                    started_at,
                    started,
                    blockers=plan.blockers,
                    warnings=[*prereq.warnings, *plan.warnings],
                    locks_acquired=_public_lock_status(lock_status),
                    session_locks_status=session_locks,
                    remote_last_change_id_before=int(remote_before.get("remote_last_change_id") or 0),
                    plan=plan,
                    backups=backups,
                )
                self._mark_merge_failed_if_possible(session, result)
                self.write_merge_report(result)
                return result

            apply_info = apply_row_merge_plan(prereq.context.medical_db_path, plan)
            final_validation = self.validate_final_remote_db(prereq.context.medical_db_path)
            if not final_validation.get("ok"):
                rollback = self._restore_remote_backup(
                    prereq.context.medical_db_path,
                    backups["remote_backup_path"],
                    reason=str(final_validation.get("reason") or "final validation failed"),
                )
                result = self._result(
                    session_id=session.emergency_session_id,
                    status="rolled_back",
                    error_code="final_validation_failed",
                    error=str(final_validation.get("reason") or "final validation failed"),
                    blockers=[_blocker("final_validation_failed", str(final_validation.get("reason") or ""))],
                    warnings=[*prereq.warnings, *plan.warnings],
                    report_path=report_path,
                    marker_path=prereq.marker_path,
                    dry_run_report_path=prereq.dry_run_report_path,
                    remote_backup_path=backups["remote_backup_path"],
                    local_backup_path=backups["local_backup_path"],
                    base_last_change_id=int(session.base_last_change_id or 0),
                    local_last_change_id=int(remote_before.get("local_last_change_id") or 0),
                    remote_last_change_id_before=int(remote_before.get("remote_last_change_id") or 0),
                    applied_change_count_estimate=plan.applied_change_count_estimate,
                    changed_tables_summary=plan.changed_tables_summary,
                    conflicts=plan.conflicts,
                    protected_summary=plan.protected_summary,
                    skipped_tables=plan.skipped_tables,
                    locks_acquired=_public_lock_status(lock_status),
                    session_locks_status=session_locks,
                    rollback_status=str(rollback.get("status") or ""),
                    started_at=started_at,
                    started=started,
                )
                self._mark_merge_failed_if_possible(session, result)
                self.write_merge_report(result)
                return result

            fresh_standby = self._legacy.create_fresh_standby_after_merge(prereq.context)
            warnings = [*prereq.warnings, *plan.warnings]
            if bool(remote_before.get("remote_changed_after_base")):
                warnings.append("remote medical DB changed after emergency base; row-level merge preserved remote-only rows")
                warnings.append("local emergency rows won RemCard conflicts")
            if not fresh_standby.get("ok"):
                warnings.append(f"fresh standby creation warning: {fresh_standby.get('reason')}")

            result = self._result(
                session_id=session.emergency_session_id,
                status="success",
                error_code="",
                error=None,
                report_path=report_path,
                marker_path=prereq.marker_path,
                dry_run_report_path=prereq.dry_run_report_path,
                remote_backup_path=backups["remote_backup_path"],
                local_backup_path=backups["local_backup_path"],
                final_remote_hash=str(final_validation.get("file_hash") or ""),
                post_quick_check_status=str(final_validation.get("quick_check") or ""),
                post_integrity_check_status=str(final_validation.get("integrity_check") or ""),
                post_foreign_key_check_status=str(final_validation.get("foreign_key_check") or ""),
                base_last_change_id=int(session.base_last_change_id or 0),
                local_last_change_id=int(remote_before.get("local_last_change_id") or 0),
                remote_last_change_id_before=int(remote_before.get("remote_last_change_id") or 0),
                remote_last_change_id_after=int(final_validation.get("last_change_id") or 0),
                applied_change_count_estimate=int(apply_info.get("applied_operations") or 0),
                changed_tables_summary=plan.changed_tables_summary,
                conflicts=plan.conflicts,
                protected_summary=plan.protected_summary,
                skipped_tables=plan.skipped_tables,
                locks_acquired=_public_lock_status(lock_status),
                session_locks_status=session_locks,
                fresh_standby_status=fresh_standby,
                warnings=warnings,
                started_at=started_at,
                started=started,
            )
            self.write_merge_report(result)
            self._legacy.mark_session_merged(session, result)
            self._legacy._clear_success_markers(session)
            archived_path = self._legacy.archive_emergency_session(session.emergency_session_id)
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
            )
            self.store.write_active_session(updated)
        except Exception:
            pass


def build_row_merge_plan(*, base_db_path: str, local_db_path: str, remote_db_path: str) -> RowMergePlan:
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
            for pk in sorted(all_keys, key=_sort_key):
                base_row = base_rows.get(pk)
                local_row = local_rows.get(pk)
                remote_row = remote_rows.get(pk)
                if base_row is None and local_row is not None:
                    if remote_row is not None and _rows_equal(local_row, remote_row):
                        continue
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
                                continue
                            blockers.append(
                                _blocker("row_pk_collision", f"local insert collides with remote {table}{pk}", table=table)
                            )
                            continue
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
                        )
                    )
                    table_summary["insert"] += 1
                    if force_new_pk:
                        table_summary["id_remap"] += 1
                    continue

                if base_row is not None and local_row is None:
                    if remote_row is None:
                        continue
                    if _is_protected_common_row(table, remote_row, protected):
                        blockers.append(_blocker("protected_common_row_delete", f"delete touches protected {table}{pk}", table=table))
                        continue
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
                        )
                    )
                    table_summary["delete"] += 1
                    continue

                if base_row is None or local_row is None or _rows_equal(base_row, local_row):
                    continue
                if remote_row is not None and _rows_equal(local_row, remote_row):
                    continue
                if remote_row is not None and _is_protected_common_row(table, remote_row, protected):
                    blockers.append(_blocker("protected_common_row_update", f"update touches protected {table}{pk}", table=table))
                    continue
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
                    )
                )
                table_summary["update"] += 1

            if any(table_summary.values()):
                summary[table] = table_summary

        if conflicts:
            warnings.append(f"local-wins conflicts detected: {len(conflicts)}")
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


def apply_row_merge_plan(remote_db_path: str, plan: RowMergePlan) -> dict[str, Any]:
    conn = _connect(remote_db_path, readonly=False)
    id_map: dict[tuple[str, Any], Any] = {}
    applied = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
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
            conn.execute("COMMIT")
            return {"applied_operations": applied, "id_remap_count": len(id_map)}
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


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
        uri = f"file:{os.path.abspath(path)}?mode=ro"
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
