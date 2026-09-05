"""Safety-сценарии: operblock_transactions."""

from __future__ import annotations

from typing import Any
from .common import PROJECT_ROOT
from pathlib import Path
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time


def _check_analyze_ui_stall_logs_classifies_nurse_backup_contention(temp_root: str) -> tuple[bool, str]:
    logs_dir = Path(temp_root, "nurse_log", "logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = logs_dir / "metrics_20260619.jsonl"
    rows = [
        {"ts": "2026-06-19T07:56:07+10:00", "metric": "backup_duration_ms", "value": 22700, "role": "nurse"},
        {"ts": "2026-06-19T07:56:08+10:00", "metric": "patient_vitals_elapsed_ms", "value": 24085, "role": "nurse"},
        {"ts": "2026-06-19T07:56:09+10:00", "metric": "event_loop_pause_ms", "value": 25252, "role": "nurse"},
    ]
    metrics_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "analyze_ui_stall_logs.py"),
            "--logs",
            str(logs_dir),
            "--date",
            "2026-06-19",
            "--role",
            "nurse",
            "--json",
        ],
        cwd=str(PROJECT_ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    if result.returncode != 0:
        return False, f"analyze_ui_stall_logs failed: {result.stderr[-500:]}"
    summary = json.loads(result.stdout)
    pauses = summary.get("ui_pauses") or []
    if not pauses:
        return False, f"analyzer did not report UI pause: {summary}"
    if pauses[0].get("classification") != "maintenance_contention_backup":
        return False, f"nurse backup contention was not classified: {pauses[0]}"
    return True, "ok"


def _check_analyze_ui_stall_logs_reports_operblock_icons_schema_drift(temp_root: str) -> tuple[bool, str]:
    logs_dir = Path(temp_root, "nurse_log", "logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = logs_dir / "metrics_20260619.jsonl"
    rows = [
        {
            "ts": "2026-06-19T08:37:01+10:00",
            "metric": "emergency_startup_failed",
            "value": 1,
            "role": "nurse",
            "reason": "settings snapshot validation failed: missing settings tables: operblock_icons",
        },
        {"ts": "2026-06-19 08:37:02", "metric": "event_loop_pause_ms", "value": 760, "role": "nurse"},
    ]
    metrics_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "analyze_ui_stall_logs.py"),
            "--logs",
            str(logs_dir),
            "--date",
            "2026-06-19",
            "--role",
            "nurse",
            "--json",
        ],
        cwd=str(PROJECT_ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    if result.returncode != 0:
        return False, f"analyze_ui_stall_logs failed: {result.stderr[-500:]}"
    summary = json.loads(result.stdout)
    if int(summary.get("settings_snapshot_schema_drift_count") or 0) < 1:
        return False, f"schema drift summary missing: {summary}"
    classifications = summary.get("classifications") or {}
    if int(classifications.get("settings_snapshot_schema_drift") or 0) < 1:
        return False, f"schema drift classification missing: {summary}"
    return True, "ok"


def _check_opblock_idle_metrics_events_exist(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    sources = "\n".join(
        (PROJECT_ROOT / rel_path).read_text(encoding="utf-8")
        for rel_path in (
            "ui/operblock_view/operblock_main_widget.py",
            "ui/main_window.py",
            "app/sqlite_shared.py",
            "services/data_service.py",
        )
    )
    required = {
        "user_idle_detected",
        "user_return_from_idle",
        "opblock_action_started",
        "opblock_action_finished",
        "sqlite_write_lock_wait_started",
        "sqlite_write_lock_wait_retry",
        "sqlite_write_lock_timeout",
        "sqlite_write_lock_acquired",
        "sqlite_write_lock_released",
        "sqlite_write_lock_stale_observed",
        "opblock_shadow_mirror_started",
        "opblock_shadow_mirror_finished",
        "opblock_shadow_mirror_failed",
        "opblock_shadow_mirror_post_commit_started",
        "opblock_shadow_mirror_post_commit_succeeded",
        "opblock_shadow_mirror_post_commit_failed",
        "opblock_shadow_mirror_retry_scheduled",
        "opblock_shadow_mirror_retry_succeeded",
        "opblock_shadow_mirror_retry_exhausted",
        "opblock_shadow_mirror_decoupled_from_write",
        "opblock_shadow_mirror_failure_did_not_fail_network_write",
        "maintenance_overlap_observed",
        "ui_pending_state_observed",
    }
    missing = sorted(name for name in required if name not in sources)
    if missing:
        return False, f"opblock idle diagnostic events missing: {missing}"
    return True, "ok"


def _check_sqlite_lock_holder_diagnostics_is_read_only(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.sqlite_shared import describe_sqlite_lock_holder

    lock_path = Path(temp_root, "archiv", "db.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": time.time() - 12.5,
        "pid": 123456,
        "host": "regression-host",
        "source": "regression_source",
    }
    raw = json.dumps(payload, ensure_ascii=True)
    lock_path.write_text(raw, encoding="utf-8")
    result = describe_sqlite_lock_holder(str(lock_path))
    after = lock_path.read_text(encoding="utf-8")
    if after != raw:
        return False, "lock holder diagnostic helper modified lock payload"
    if not lock_path.exists():
        return False, "lock holder diagnostic helper removed lock file"
    if not result.get("readable") or result.get("holder_pid") != 123456:
        return False, f"lock holder diagnostic payload mismatch: {result}"
    missing = describe_sqlite_lock_holder(str(lock_path.with_name("missing.lock")))
    if missing.get("readable") or missing.get("reason") != "missing":
        return False, f"missing lock diagnostic result mismatch: {missing}"
    return True, "ok"


def _check_uiwatchdog_opblock_context_fields(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    source = (PROJECT_ROOT / "ui/main_window.py").read_text(encoding="utf-8")
    required = (
        "active_opblock_action",
        "active_sqlite_operation",
        "active_foreground_lease",
        "last_user_action",
        "idle_before_action_ms",
        "current_operation_case_id",
        "current_admission_id",
        "current_table_code",
        "lock_wait_operation",
        "lock_holder_pid",
        "lock_holder_host",
        "lock_holder_source",
        "shadow_mirror_active",
        "ui_pending_action",
        "ui_pending_since_ms",
    )
    missing = [token for token in required if token not in source]
    if missing:
        return False, f"UIWatchdog opblock diagnostics fields missing: {missing}"
    return True, "ok"


def _check_opblock_idle_tracker_does_not_change_behavior(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    source = (PROJECT_ROOT / "ui/operblock_view/operblock_main_widget.py").read_text(encoding="utf-8")
    forbidden = (
        "should_defer_background_io",
        "sqlite3.connect",
        "recovery",
    )
    present = [token for token in forbidden if token in source]
    if present:
        return False, f"Stage 1 idle tracker introduced behavior/policy token(s): {present}"
    if "self.data_service.enqueue_write(" not in source or "write_metadata=write_metadata" not in source:
        return False, "opblock write path no longer delegates through DataService.enqueue_write with write metadata"
    if "def diagnostic_snapshot" not in source or '"user_return_from_idle"' not in source:
        return False, "opblock idle tracker diagnostics are missing"
    return True, "ok"


def _check_analyze_opblock_idle_stalls_script_exists(temp_root: str) -> tuple[bool, str]:
    logs_dir = Path(temp_root, "opblock_idle_logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = logs_dir / "metrics_20260626.jsonl"
    rows = [
        {
            "ts": "2026-06-26T11:11:30+10:00",
            "metric": "user_return_from_idle",
            "idle_ms": 521000,
            "first_action": "operblock_undo_last_action",
        },
        {
            "ts": "2026-06-26T11:11:31+10:00",
            "metric": "opblock_action_started",
            "action": "operblock_undo_last_action",
            "request_id": "regression",
        },
        {
            "ts": "2026-06-26T11:11:32+10:00",
            "metric": "sqlite_write_lock_wait_retry",
            "total_wait_ms": 30000,
            "lock_holder_pid": 5024,
            "lock_holder_host": "operblok1",
            "lock_holder_source": "operblock_undo_last_action",
        },
        {"ts": "2026-06-26T11:11:33+10:00", "metric": "event_loop_pause_ms", "value": 30120},
    ]
    metrics_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    help_result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "analyze_opblock_idle_stalls.py"), "--help"],
        cwd=str(PROJECT_ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    if help_result.returncode != 0:
        return False, f"analyze_opblock_idle_stalls --help failed: {help_result.stderr[-500:]}"
    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "analyze_opblock_idle_stalls.py"),
            "--logs",
            str(logs_dir),
            "--json",
        ],
        cwd=str(PROJECT_ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    if result.returncode != 0:
        return False, f"analyze_opblock_idle_stalls failed: {result.stderr[-500:]}"
    summary = json.loads(result.stdout)
    incidents = summary.get("incidents") or []
    if not incidents or incidents[0].get("classification") != "sqlite_write_lock_wait":
        return False, f"opblock idle analyzer did not classify lock wait: {summary}"
    return True, "ok"


def _check_opblock_stage1_no_sqlite_profile_changes(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from rem_card.app.sqlite_shared import _resolve_sqlite_profile_settings

    settings = _resolve_sqlite_profile_settings("network")
    expected = {"journal_mode": "DELETE", "synchronous": "EXTRA", "mmap_mb": 0}
    mismatches = {key: settings.get(key) for key, value in expected.items() if settings.get(key) != value}
    if mismatches:
        return False, f"SQLite network profile changed: {mismatches}"
    return True, "ok"


def _check_opblock_foreground_resume_lease_events_exist(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    sources = "\n".join(
        (PROJECT_ROOT / rel_path).read_text(encoding="utf-8")
        for rel_path in (
            "app/foreground_activity.py",
            "services/data_service.py",
            "ui/main_window.py",
            "ui/operblock_view/operblock_main_widget.py",
            "scripts/analyze_opblock_idle_stalls.py",
        )
    )
    required = {
        "foreground_resume_lease_started",
        "foreground_resume_lease_finished",
        "maintenance_deferred_for_foreground_resume",
        "maintenance_resume_after_foreground",
    }
    missing = sorted(name for name in required if name not in sources)
    if missing:
        return False, f"foreground resume diagnostic events missing: {missing}"
    return True, "ok"


def _check_opblock_resume_lease_starts_after_idle(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from rem_card.app.foreground_activity import (
        _reset_foreground_activity_for_tests,
        foreground_resume_snapshot,
        start_foreground_resume_lease,
    )

    _reset_foreground_activity_for_tests()
    lease = start_foreground_resume_lease(
        role="operblock_planned",
        idle_ms=301000,
        first_action="operblock_undo_last_action",
        current_screen="protocol",
        admission_id=11,
        operation_case_id=22,
        table_code="planned",
    )
    snapshot = foreground_resume_snapshot()
    _reset_foreground_activity_for_tests()
    if not lease or not snapshot.get("active"):
        return False, f"foreground resume lease was not created: lease={lease} snapshot={snapshot}"
    if (snapshot.get("lease") or {}).get("first_action") != "operblock_undo_last_action":
        return False, f"foreground resume lease first action mismatch: {snapshot}"
    return True, "ok"


def _check_opblock_resume_lease_does_not_start_without_idle(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from rem_card.app.foreground_activity import (
        _reset_foreground_activity_for_tests,
        foreground_resume_snapshot,
        start_foreground_resume_lease,
    )

    _reset_foreground_activity_for_tests()
    lease = start_foreground_resume_lease(
        role="operblock_planned",
        idle_ms=299000,
        first_action="operblock_undo_last_action",
        current_screen="protocol",
    )
    snapshot = foreground_resume_snapshot()
    _reset_foreground_activity_for_tests()
    if lease or snapshot.get("active"):
        return False, f"foreground resume lease started below idle threshold: lease={lease} snapshot={snapshot}"
    return True, "ok"


def _check_maintenance_deferred_during_opblock_resume_lease(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from rem_card.app.foreground_activity import (
        _reset_foreground_activity_for_tests,
        should_defer_for_foreground_resume,
        start_foreground_resume_lease,
    )

    _reset_foreground_activity_for_tests()
    start_foreground_resume_lease(
        role="operblock",
        idle_ms=360000,
        first_action="operblock_open_protocol:10",
        current_screen="board",
        operation_case_id=10,
    )
    decision = should_defer_for_foreground_resume(
        "daily_backup_cleanup",
        source="regression",
        write_queue_idle=True,
        active_foreground_action=True,
        active_opblock_action="operblock_open_protocol:10",
    )
    _reset_foreground_activity_for_tests()
    if not decision.get("defer") or decision.get("reason") != "foreground_resume_lease":
        return False, f"maintenance was not deferred during foreground lease: {decision}"
    return True, "ok"


def _check_maintenance_not_deferred_forever(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from rem_card.app.foreground_activity import (
        _reset_foreground_activity_for_tests,
        should_defer_for_foreground_resume,
        start_foreground_resume_lease,
    )

    _reset_foreground_activity_for_tests()
    start_foreground_resume_lease(
        role="operblock",
        idle_ms=360000,
        first_action="operblock_user_refresh",
        current_screen="board",
    )
    first = should_defer_for_foreground_resume(
        "daily_backup_cleanup",
        source="regression",
        max_defer_ms=1,
        write_queue_idle=True,
        active_foreground_action=False,
    )
    time.sleep(0.05)
    second = should_defer_for_foreground_resume(
        "daily_backup_cleanup",
        source="regression",
        max_defer_ms=1,
        write_queue_idle=True,
        active_foreground_action=False,
    )
    _reset_foreground_activity_for_tests()
    if not first.get("defer"):
        return False, f"initial maintenance decision was not deferred: {first}"
    if second.get("defer") or second.get("reason") != "max_defer_safe_window":
        return False, f"maintenance did not escape deferral after max age: {second}"
    return True, "ok"


def _check_shadow_mirror_deferred_during_foreground_resume(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    source = (PROJECT_ROOT / "services/data_service.py").read_text(encoding="utf-8")
    required = (
        "opblock_shadow_mirror_deferred_for_foreground_resume",
        "_defer_opblock_shadow_mirror_if_needed",
        "_drain_deferred_opblock_shadow_mirror",
        "opblock_shadow_mirror_deferred_resume",
    )
    missing = [token for token in required if token not in source]
    if missing:
        return False, f"shadow mirror foreground resume deferral missing: {missing}"
    if "mirror_active_operblock_cases_from_network_db(self.db" not in source:
        return False, "shadow mirror write logic no longer calls existing mirror implementation"
    return True, "ok"


def _check_uiwatchdog_has_foreground_resume_context(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    source = (PROJECT_ROOT / "ui/main_window.py").read_text(encoding="utf-8")
    required = (
        "active_foreground_resume_lease",
        "foreground_lease_age_ms",
        "foreground_lease_reason",
        "deferred_maintenance_tasks",
        "first_action_after_idle",
        "maintenance_task_waiting_to_start",
        "maintenance_task_deferred_count",
    )
    missing = [token for token in required if token not in source]
    if missing:
        return False, f"UIWatchdog foreground resume fields missing: {missing}"
    return True, "ok"


def _check_analyzer_understands_foreground_resume(temp_root: str) -> tuple[bool, str]:
    logs_dir = Path(temp_root, "opblock_foreground_resume_logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = logs_dir / "metrics_20260626.jsonl"
    rows = [
        {
            "ts": "2026-06-26T11:11:30+10:00",
            "metric": "user_return_from_idle",
            "idle_ms": 521000,
            "first_action": "operblock_undo_last_action",
        },
        {
            "ts": "2026-06-26T11:11:30.100000+10:00",
            "metric": "foreground_resume_lease_started",
            "lease_id": "lease-regression",
            "suppress_maintenance_for_ms": 90000,
        },
        {
            "ts": "2026-06-26T11:11:31+10:00",
            "metric": "maintenance_deferred_for_foreground_resume",
            "task": "daily_backup_cleanup",
            "foreground_lease_id": "lease-regression",
        },
        {
            "ts": "2026-06-26T11:11:32+10:00",
            "metric": "opblock_shadow_mirror_deferred_for_foreground_resume",
            "foreground_lease_id": "lease-regression",
        },
        {
            "ts": "2026-06-26T11:12:45+10:00",
            "metric": "maintenance_resume_after_foreground",
            "task": "daily_backup_cleanup",
            "foreground_lease_id": "lease-regression",
        },
    ]
    metrics_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "analyze_opblock_idle_stalls.py"),
            "--logs",
            str(logs_dir),
            "--json",
        ],
        cwd=str(PROJECT_ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    if result.returncode != 0:
        return False, f"foreground resume analyzer failed: {result.stderr[-500:]}"
    summary = json.loads(result.stdout)
    incidents = summary.get("incidents") or []
    if not incidents or incidents[0].get("classification") != "foreground_resume_protected":
        return False, f"foreground resume analyzer classification mismatch: {summary}"
    if "daily_backup_cleanup" not in (incidents[0].get("deferred_maintenance") or []):
        return False, f"foreground resume analyzer deferred task missing: {summary}"
    return True, "ok"


def _check_opblock_stage2_no_sqlite_profile_changes(temp_root: str) -> tuple[bool, str]:
    return _check_opblock_stage1_no_sqlite_profile_changes(temp_root)


def _check_opblock_interactive_write_timeout_constant_exists(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from rem_card.app.sqlite_shared import OPBLOCK_INTERACTIVE_WRITE_LOCK_TIMEOUT_MS, OpBlockInteractiveWriteBusyTimeout

    if not (5000 <= int(OPBLOCK_INTERACTIVE_WRITE_LOCK_TIMEOUT_MS) <= 8000):
        return False, f"interactive opblock timeout outside 5-8s: {OPBLOCK_INTERACTIVE_WRITE_LOCK_TIMEOUT_MS}"
    if not issubclass(OpBlockInteractiveWriteBusyTimeout, RuntimeError):
        return False, "interactive busy timeout must be a controlled RuntimeError, not raw sqlite error"
    return True, "ok"


def _check_opblock_write_metadata_marks_interactive_operations(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    sources = "\n".join(
        (PROJECT_ROOT / rel_path).read_text(encoding="utf-8")
        for rel_path in (
            "services/data_service.py",
            "data/dao/db_manager.py",
            "ui/operblock_view/operblock_main_widget.py",
        )
    )
    required = (
        "_opblock_interactive_write_metadata",
        "write_metadata_context",
        "write_metadata=write_metadata",
        '"interactive": True',
        '"request_id"',
        '"foreground_lease_id"',
        "OPBLOCK_INTERACTIVE_WRITE_LOCK_TIMEOUT_MS",
    )
    missing = [token for token in required if token not in sources]
    if missing:
        return False, f"interactive opblock write metadata path missing: {missing}"
    return True, "ok"


def _interactive_opblock_write_options(**extra) -> dict[str, Any]:
    from rem_card.app.sqlite_shared import OPBLOCK_INTERACTIVE_WRITE_LOCK_TIMEOUT_MS

    payload = {
        "interactive": True,
        "role": "operblock",
        "request_id": "regression-request",
        "foreground_lease_id": "regression-lease",
        "idle_before_action_ms": 360000,
        "timeout_ms": OPBLOCK_INTERACTIVE_WRITE_LOCK_TIMEOUT_MS,
    }
    payload.update(extra)
    return payload


def _make_stage3_opblock_manager(temp_root: str, name: str):
    from rem_card.app.db_runtime_context import build_operblock_offline_runtime_context
    from rem_card.data.dao.db_manager import DatabaseManager

    context = build_operblock_offline_runtime_context(os.path.join(temp_root, name))
    return DatabaseManager(context.medical_db_path, context.medical_db_path, runtime_context=context)


def _check_sqlite_begin_immediate_timeout_is_bounded_for_interactive_opblock(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.sqlite_shared import OPBLOCK_INTERACTIVE_WRITE_LOCK_TIMEOUT_MS, OpBlockInteractiveWriteBusyTimeout

    manager = _make_stage3_opblock_manager(temp_root, "opblock_interactive_begin_timeout")
    db_path = manager.db_path
    try:
        Path(manager.medical_db_lock_path).unlink(missing_ok=True)
    except Exception:
        pass
    blocker = sqlite3.connect(db_path, isolation_level=None, timeout=5.0)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        started = time.perf_counter()
        try:
            with manager.write_metadata_context(_interactive_opblock_write_options(operation_case_id=1)):
                manager.run_write_operation(
                    lambda cursor: cursor.execute(
                        "INSERT OR REPLACE INTO meta(key, value) VALUES ('opblock_stage3_timeout_probe', 'blocked')"
                    ),
                    source="operblock_undo_last_action",
                )
            return False, "interactive opblock write unexpectedly succeeded while external BEGIN IMMEDIATE was held"
        except OpBlockInteractiveWriteBusyTimeout as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            if elapsed_ms > 8500:
                return False, f"interactive timeout exceeded bound: elapsed_ms={elapsed_ms:.1f}, exc={exc}"
            if exc.phase != "begin_immediate_timeout":
                return False, f"expected begin_immediate_timeout, got {exc.phase}"
            if "database is locked" in str(exc).lower():
                return False, f"user-facing message leaked raw sqlite text: {exc}"
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()

        with manager.write_metadata_context(_interactive_opblock_write_options(operation_case_id=1)):
            manager.run_write_operation(
                lambda cursor: cursor.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES ('opblock_stage3_timeout_probe', 'retry_ok')"
                ),
                source="operblock_undo_last_action",
            )
        row = manager.fetch_one_remcard("SELECT value FROM meta WHERE key='opblock_stage3_timeout_probe'")
        if not row or row[0] != "retry_ok":
            return False, f"retry after releasing lock did not commit: {row}"
        if OPBLOCK_INTERACTIVE_WRITE_LOCK_TIMEOUT_MS > 8000:
            return False, "interactive timeout constant exceeds upper bound"
        return True, "ok"
    finally:
        try:
            blocker.close()
        except Exception:
            pass
        manager.close()


def _check_opblock_undo_last_action_busy_timeout(temp_root: str) -> tuple[bool, str]:
    return _check_sqlite_begin_immediate_timeout_is_bounded_for_interactive_opblock(temp_root)


def _check_busy_timeout_does_not_trigger_recovery(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from rem_card.app.db_access_classifier import classify_database_access
    from rem_card.app.runtime_outage import runtime_outage_transition_allowed
    from rem_card.app.sqlite_shared import OpBlockInteractiveWriteBusyTimeout

    exc = OpBlockInteractiveWriteBusyTimeout(
        operation_name="operblock_undo_last_action",
        source="operblock_undo_last_action",
        timeout_ms=7000,
        total_wait_ms=7003,
        phase="begin_immediate_timeout",
    )
    classification = classify_database_access(exc)
    if classification.category != "locked_busy":
        return False, f"busy timeout was not classified as locked_busy: {classification}"
    if runtime_outage_transition_allowed(classification.category):
        return False, "controlled opblock busy timeout must not trigger runtime recovery/outage transition by default"
    db_manager_source = (PROJECT_ROOT / "data/dao/db_manager.py").read_text(encoding="utf-8")
    if "OpBlockInteractiveWriteBusyTimeout" in db_manager_source and "recover_shared_db" in db_manager_source:
        return False, "controlled busy timeout is wired to recovery"
    return True, "ok"


def _check_file_lock_timeout_does_not_delete_lock(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.sqlite_shared import OpBlockInteractiveWriteBusyTimeout

    manager = _make_stage3_opblock_manager(temp_root, "opblock_interactive_file_lock_timeout")
    lock_path = Path(manager.medical_db_lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": time.time(),
        "pid": 987654,
        "host": "regression-host",
        "user_id": "external-holder",
        "source": "external_begin_immediate",
        "thread_id": 1,
    }
    raw = json.dumps(payload, ensure_ascii=True)
    lock_path.write_text(raw, encoding="utf-8")
    try:
        try:
            with manager.write_metadata_context(_interactive_opblock_write_options(operation_case_id=2)):
                manager.run_write_operation(
                    lambda cursor: cursor.execute(
                        "INSERT OR REPLACE INTO meta(key, value) VALUES ('opblock_stage3_file_lock_probe', 'blocked')"
                    ),
                    source="operblock_undo_last_action",
                )
            return False, "interactive opblock write unexpectedly acquired externally held file lock"
        except OpBlockInteractiveWriteBusyTimeout as exc:
            if exc.phase != "file_lock_timeout":
                return False, f"expected file_lock_timeout, got {exc.phase}"
        after = lock_path.read_text(encoding="utf-8") if lock_path.exists() else ""
        if after != raw:
            return False, "Stage 3 file lock timeout modified or deleted the existing lock file"
        return True, "ok"
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass
        manager.close()


def _check_non_interactive_write_behavior_unchanged(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    source = (PROJECT_ROOT / "app/sqlite_shared.py").read_text(encoding="utf-8")
    if "is_interactive_opblock" not in source:
        return False, "SQLite write controller no longer guards bounded timeout behind interactive opblock metadata"
    if "PRAGMA busy_timeout = {begin_busy_timeout_ms}" not in source:
        return False, "interactive begin timeout no longer uses temporary per-write busy_timeout"
    if "PRAGMA busy_timeout = {int(original_busy_timeout_ms)}" not in source:
        return False, "temporary interactive busy_timeout is not restored"
    return True, "ok"


def _check_ui_busy_timeout_message_is_controlled(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from rem_card.app.sqlite_shared import OpBlockInteractiveWriteBusyTimeout

    exc = OpBlockInteractiveWriteBusyTimeout(
        operation_name="operblock_undo_last_action",
        source="operblock_undo_last_action",
        timeout_ms=7000,
        total_wait_ms=7002,
        phase="begin_immediate_timeout",
        holder={"holder_pid": 5024, "holder_host": "operblok1", "holder_source": "periodic_backup"},
        sqlite_error_message_sanitized="database is locked",
    )
    text = str(exc)
    forbidden = ("Traceback", "sqlite3.", "database is locked", "BEGIN IMMEDIATE")
    leaked = [token for token in forbidden if token in text]
    if leaked:
        return False, f"controlled busy timeout message leaks technical text: {leaked}: {text}"
    if "Действие не выполнено" not in text or "PID 5024" not in text:
        return False, f"controlled busy timeout message missing required guidance/holder: {text}"
    return True, "ok"


def _check_ui_pending_cleared_after_busy_timeout(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    source = (PROJECT_ROOT / "ui/operblock_view/operblock_main_widget.py").read_text(encoding="utf-8")
    required = (
        "ui_pending_cleared_after_busy_timeout",
        "_is_interactive_busy_timeout",
        'self._finish_opblock_action_diagnostics(\n                    action_info,\n                    self._diagnostic_result_for_error(exc),',
        '"busy_timeout"',
    )
    missing = [token for token in required if token not in source]
    if missing:
        return False, f"UI busy timeout pending cleanup tokens missing: {missing}"
    return True, "ok"


def _check_opblock_stage3_sqlite_profile_unchanged(temp_root: str) -> tuple[bool, str]:
    return _check_opblock_stage1_no_sqlite_profile_changes(temp_root)


def _check_analyzer_understands_opblock_busy_timeout(temp_root: str) -> tuple[bool, str]:
    logs_dir = Path(temp_root, "opblock_busy_timeout_logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = logs_dir / "metrics_20260626.jsonl"
    rows = [
        {
            "ts": "2026-06-26T11:11:30+10:00",
            "metric": "user_return_from_idle",
            "idle_ms": 521000,
            "first_action": "operblock_undo_last_action",
        },
        {
            "ts": "2026-06-26T11:11:30.100000+10:00",
            "metric": "foreground_resume_lease_started",
            "lease_id": "lease-regression",
            "suppress_maintenance_for_ms": 90000,
        },
        {
            "ts": "2026-06-26T11:11:31+10:00",
            "metric": "opblock_action_started",
            "action": "operblock_undo_last_action",
            "request_id": "regression",
            "foreground_lease_id": "lease-regression",
        },
        {
            "ts": "2026-06-26T11:11:38+10:00",
            "metric": "sqlite_write_lock_timeout",
            "total_wait_ms": 7003,
            "timeout_ms": 7000,
            "phase": "begin_immediate_timeout",
            "lock_holder_pid": 5024,
            "lock_holder_host": "operblok1",
            "lock_holder_source": "periodic_backup",
        },
        {
            "ts": "2026-06-26T11:11:38.100000+10:00",
            "metric": "opblock_action_finished",
            "result": "busy_timeout",
            "action": "operblock_undo_last_action",
        },
        {
            "ts": "2026-06-26T11:11:38.200000+10:00",
            "metric": "ui_pending_cleared_after_busy_timeout",
            "action": "operblock_undo_last_action",
        },
    ]
    metrics_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "analyze_opblock_idle_stalls.py"),
            "--logs",
            str(logs_dir),
            "--json",
        ],
        cwd=str(PROJECT_ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    if result.returncode != 0:
        return False, f"busy timeout analyzer failed: {result.stderr[-500:]}"
    summary = json.loads(result.stdout)
    incidents = summary.get("incidents") or []
    if not incidents or incidents[0].get("classification") != "begin_immediate_timeout":
        return False, f"busy timeout analyzer classification mismatch: {summary}"
    if incidents[0].get("ui_result") != "busy_timeout":
        return False, f"busy timeout analyzer UI result mismatch: {summary}"
    return True, "ok"


def _check_file_write_lock_local_dead_pid_cleanup_constant_or_helper_exists(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from rem_card.app.sqlite_shared import is_local_pid_alive

    source = (PROJECT_ROOT / "app/sqlite_shared.py").read_text(encoding="utf-8")
    required = (
        "def is_local_pid_alive",
        "_read_lock_snapshot",
        "_same_lock_snapshot",
        "sqlite_write_lock_local_dead_pid_detected",
        "sqlite_write_lock_stale_removed",
        "sqlite_write_lock_stale_cleanup_skipped",
        "sqlite_write_lock_stale_cleanup_failed",
        "changed_during_cleanup_check",
        "age-only cleanup is disabled",
    )
    missing = [token for token in required if token not in source]
    if missing:
        return False, f"Stage 4 local dead-PID cleanup helper/logic missing: {missing}"
    if is_local_pid_alive(os.getpid()) is not True:
        return False, "is_local_pid_alive must report current PID as alive"
    return True, "ok"


def _write_stage4_file_lock(lock_path: Path, *, pid: int, host: str, source: str = "operblock_undo_last_action") -> str:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": time.time(),
        "pid": int(pid),
        "host": host,
        "user_id": "stage4-holder",
        "source": source,
        "thread_id": 101,
    }
    raw = json.dumps(payload, ensure_ascii=True)
    lock_path.write_text(raw, encoding="utf-8")
    return raw


def _capture_sqlite_shared_metrics():
    import rem_card.app.sqlite_shared as sqlite_shared

    events: list[dict[str, Any]] = []
    original_record_metric = sqlite_shared.record_metric

    def fake_record_metric(name, value=None, **fields):
        payload = {"metric": str(name), "value": value}
        payload.update(fields)
        events.append(payload)

    sqlite_shared.record_metric = fake_record_metric
    return sqlite_shared, events, original_record_metric


def _restore_sqlite_shared_metrics(sqlite_shared, original_record_metric) -> None:
    sqlite_shared.record_metric = original_record_metric


def _metric_reasons(events: list[dict[str, Any]], metric: str) -> list[str]:
    return [str(event.get("reason") or "") for event in events if event.get("metric") == metric]


def _check_file_write_lock_same_host_dead_pid_removed(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.sqlite_shared import FileWriteLock

    lock_path = Path(temp_root, "stage4_dead_pid", "db.lock")
    current_host = socket.gethostname()
    dead_pid = 99999999
    _write_stage4_file_lock(lock_path, pid=dead_pid, host=current_host)
    sqlite_shared, events, original_record_metric = _capture_sqlite_shared_metrics()
    original_pid_checker = sqlite_shared.is_local_pid_alive
    try:
        if original_pid_checker(dead_pid) is not False:
            sqlite_shared.is_local_pid_alive = lambda pid: False
        lock = FileWriteLock(str(lock_path), stale_timeout_sec=3600.0)
        acquired = lock.acquire(
            owner_id="stage4-waiter",
            source="operblock_undo_last_action",
            metric_context={"request_id": "stage4-request", "role": "operblock"},
        )
        if not acquired:
            return False, "same-host dead-PID lock was not cleaned and acquired"
        try:
            payload = json.loads(lock_path.read_text(encoding="utf-8"))
            if payload.get("user_id") != "stage4-waiter":
                return False, f"lock was not reacquired by waiter after cleanup: {payload}"
            removed = [event for event in events if event.get("metric") == "sqlite_write_lock_stale_removed"]
            if not removed or removed[-1].get("reason") != "local_dead_pid":
                return False, f"stale_removed local_dead_pid metric missing: {events}"
            detected = [event for event in events if event.get("metric") == "sqlite_write_lock_local_dead_pid_detected"]
            if not detected:
                return False, f"local dead PID detection metric missing: {events}"
            if removed[-1].get("request_id") != "stage4-request" or removed[-1].get("role") != "operblock":
                return False, f"cleanup metric lost opblock context: {removed[-1]}"
            return True, "ok"
        finally:
            lock.release()
    finally:
        sqlite_shared.is_local_pid_alive = original_pid_checker
        _restore_sqlite_shared_metrics(sqlite_shared, original_record_metric)
        try:
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass


def _check_file_write_lock_same_host_live_pid_not_removed(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.sqlite_shared import FileWriteLock

    lock_path = Path(temp_root, "stage4_live_pid", "db.lock")
    raw = _write_stage4_file_lock(lock_path, pid=os.getpid(), host=socket.gethostname())
    sqlite_shared, events, original_record_metric = _capture_sqlite_shared_metrics()
    try:
        lock = FileWriteLock(str(lock_path), stale_timeout_sec=3600.0)
        if lock.acquire(owner_id="stage4-waiter", source="operblock_undo_last_action"):
            lock.release()
            return False, "same-host live PID lock was incorrectly removed"
        if lock_path.read_text(encoding="utf-8") != raw:
            return False, "same-host live PID lock changed or was removed"
        if "pid_alive" not in _metric_reasons(events, "sqlite_write_lock_stale_cleanup_skipped"):
            return False, f"pid_alive skip metric missing: {events}"
        return True, "ok"
    finally:
        _restore_sqlite_shared_metrics(sqlite_shared, original_record_metric)
        try:
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass


def _check_file_write_lock_other_host_not_removed(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.sqlite_shared import FileWriteLock

    lock_path = Path(temp_root, "stage4_other_host", "db.lock")
    other_host = f"{socket.gethostname()}-other"
    raw = _write_stage4_file_lock(lock_path, pid=99999999, host=other_host)
    sqlite_shared, events, original_record_metric = _capture_sqlite_shared_metrics()
    original_pid_checker = sqlite_shared.is_local_pid_alive
    try:
        sqlite_shared.is_local_pid_alive = lambda pid: False
        lock = FileWriteLock(str(lock_path), stale_timeout_sec=3600.0)
        if lock.acquire(owner_id="stage4-waiter", source="operblock_undo_last_action"):
            lock.release()
            return False, "other-host lock was incorrectly removed"
        if lock_path.read_text(encoding="utf-8") != raw:
            return False, "other-host lock changed or was removed"
        if "other_host" not in _metric_reasons(events, "sqlite_write_lock_stale_cleanup_skipped"):
            return False, f"other_host skip metric missing: {events}"
        return True, "ok"
    finally:
        sqlite_shared.is_local_pid_alive = original_pid_checker
        _restore_sqlite_shared_metrics(sqlite_shared, original_record_metric)
        try:
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass


def _check_file_write_lock_unknown_pid_not_removed(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.sqlite_shared import FileWriteLock

    lock_path = Path(temp_root, "stage4_unknown_pid", "db.lock")
    raw = _write_stage4_file_lock(lock_path, pid=99999999, host=socket.gethostname())
    sqlite_shared, events, original_record_metric = _capture_sqlite_shared_metrics()
    original_pid_checker = sqlite_shared.is_local_pid_alive
    try:
        sqlite_shared.is_local_pid_alive = lambda pid: None
        lock = FileWriteLock(str(lock_path), stale_timeout_sec=3600.0)
        if lock.acquire(owner_id="stage4-waiter", source="operblock_undo_last_action"):
            lock.release()
            return False, "unknown PID lock was incorrectly removed"
        if lock_path.read_text(encoding="utf-8") != raw:
            return False, "unknown PID lock changed or was removed"
        if "pid_unknown" not in _metric_reasons(events, "sqlite_write_lock_stale_cleanup_skipped"):
            return False, f"pid_unknown skip metric missing: {events}"
        return True, "ok"
    finally:
        sqlite_shared.is_local_pid_alive = original_pid_checker
        _restore_sqlite_shared_metrics(sqlite_shared, original_record_metric)
        try:
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass


def _check_file_write_lock_unreadable_or_parse_error_not_removed(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.sqlite_shared import FileWriteLock

    lock_path = Path(temp_root, "stage4_parse_error", "db.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    raw = "{not-json"
    lock_path.write_text(raw, encoding="utf-8")
    sqlite_shared, events, original_record_metric = _capture_sqlite_shared_metrics()
    try:
        lock = FileWriteLock(str(lock_path), stale_timeout_sec=0.0)
        if lock.acquire(owner_id="stage4-waiter", source="operblock_undo_last_action"):
            lock.release()
            return False, "parse-error lock was incorrectly removed"
        if lock_path.read_text(encoding="utf-8") != raw:
            return False, "parse-error lock changed or was removed"
        if "parse_error" not in _metric_reasons(events, "sqlite_write_lock_stale_cleanup_skipped"):
            return False, f"parse_error skip metric missing: {events}"
        return True, "ok"
    finally:
        _restore_sqlite_shared_metrics(sqlite_shared, original_record_metric)
        try:
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass


def _check_file_write_lock_changed_during_cleanup_not_removed(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.sqlite_shared import FileWriteLock

    lock_path = Path(temp_root, "stage4_changed", "db.lock")
    _write_stage4_file_lock(lock_path, pid=99999999, host=socket.gethostname(), source="old_holder")
    changed_raw = ""

    class MutatingFileWriteLock(FileWriteLock):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.snapshot_reads = 0

        def _read_lock_snapshot(self):
            nonlocal changed_raw
            snapshot = super()._read_lock_snapshot()
            self.snapshot_reads += 1
            if self.snapshot_reads == 1:
                changed_raw = _write_stage4_file_lock(
                    lock_path,
                    pid=99999999,
                    host=socket.gethostname(),
                    source="new_holder",
                )
            return snapshot

    sqlite_shared, events, original_record_metric = _capture_sqlite_shared_metrics()
    original_pid_checker = sqlite_shared.is_local_pid_alive
    try:
        sqlite_shared.is_local_pid_alive = lambda pid: False
        lock = MutatingFileWriteLock(str(lock_path), stale_timeout_sec=3600.0)
        if lock.acquire(owner_id="stage4-waiter", source="operblock_undo_last_action"):
            lock.release()
            return False, "changed lock was incorrectly removed"
        if lock_path.read_text(encoding="utf-8") != changed_raw:
            return False, "changed lock content was not preserved"
        if "changed_during_cleanup_check" not in _metric_reasons(events, "sqlite_write_lock_stale_cleanup_skipped"):
            return False, f"changed_during_cleanup_check skip metric missing: {events}"
        return True, "ok"
    finally:
        sqlite_shared.is_local_pid_alive = original_pid_checker
        _restore_sqlite_shared_metrics(sqlite_shared, original_record_metric)
        try:
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass


def _check_stage4_does_not_enable_recovery_on_locked_busy(temp_root: str) -> tuple[bool, str]:
    ok, details = _check_busy_timeout_does_not_trigger_recovery(temp_root)
    if not ok:
        return ok, details
    source = (PROJECT_ROOT / "app/sqlite_shared.py").read_text(encoding="utf-8")
    cleanup_start = source.find("def _cleanup_local_dead_pid_lock")
    cleanup_end = source.find("def _is_stale", cleanup_start)
    cleanup_source = source[cleanup_start:cleanup_end]
    forbidden = ("recover_shared_db", "restore_from_best_available_source", "quarantine_corrupted_db_file")
    present = [token for token in forbidden if token in cleanup_source]
    if present:
        return False, f"Stage 4 cleanup path must not trigger recovery: {present}"
    return True, "ok"


def _check_stage4_does_not_change_sqlite_profile(temp_root: str) -> tuple[bool, str]:
    return _check_opblock_stage1_no_sqlite_profile_changes(temp_root)


def _check_opblock_interactive_timeout_still_works_when_cleanup_skipped(temp_root: str) -> tuple[bool, str]:
    return _check_file_lock_timeout_does_not_delete_lock(temp_root)


def _check_analyzer_understands_local_dead_pid_cleanup(temp_root: str) -> tuple[bool, str]:
    logs_dir = Path(temp_root, "opblock_stage4_cleanup_logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = logs_dir / "metrics_20260626.jsonl"
    rows = [
        {
            "ts": "2026-06-26T11:13:42+10:00",
            "metric": "user_return_from_idle",
            "idle_ms": 520000,
            "first_action": "operblock_undo_last_action",
        },
        {
            "ts": "2026-06-26T11:13:43+10:00",
            "metric": "sqlite_write_lock_local_dead_pid_detected",
            "lock_path": "archiv\\db.lock",
            "holder_pid": 5024,
            "holder_host": "operblok1",
            "holder_source": "operblock_undo_last_action",
            "operation_name": "operblock_undo_last_action",
        },
        {
            "ts": "2026-06-26T11:13:43.050000+10:00",
            "metric": "sqlite_write_lock_stale_removed",
            "reason": "local_dead_pid",
            "lock_path": "archiv\\db.lock",
            "holder_pid": 5024,
            "holder_host": "operblok1",
            "holder_source": "operblock_undo_last_action",
            "operation_name": "operblock_undo_last_action",
        },
        {
            "ts": "2026-06-26T11:13:44+10:00",
            "metric": "opblock_action_finished",
            "result": "success",
            "action": "operblock_undo_last_action",
        },
        {
            "ts": "2026-06-26T11:20:00+10:00",
            "metric": "user_return_from_idle",
            "idle_ms": 540000,
            "first_action": "operblock_undo_last_action",
        },
        {
            "ts": "2026-06-26T11:20:01+10:00",
            "metric": "sqlite_write_lock_stale_cleanup_skipped",
            "reason": "other_host",
            "lock_path": "archiv\\db.lock",
            "holder_pid": 5024,
            "holder_host": "other-host",
            "holder_source": "periodic_backup",
            "operation_name": "operblock_undo_last_action",
        },
        {
            "ts": "2026-06-26T11:20:08+10:00",
            "metric": "sqlite_write_lock_timeout",
            "phase": "file_lock_timeout",
            "total_wait_ms": 7002,
        },
    ]
    metrics_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "analyze_opblock_idle_stalls.py"),
            "--logs",
            str(logs_dir),
            "--json",
        ],
        cwd=str(PROJECT_ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    if result.returncode != 0:
        return False, f"Stage 4 cleanup analyzer failed: {result.stderr[-500:]}"
    summary = json.loads(result.stdout)
    classifications = [incident.get("classification") for incident in (summary.get("incidents") or [])]
    if "local_dead_pid_lock_removed" not in classifications:
        return False, f"cleanup removed classification missing: {summary}"
    if "other_host_lock_wait" not in classifications:
        return False, f"cleanup skipped classification missing: {summary}"
    first = (summary.get("incidents") or [{}])[0]
    if first.get("cleanup") != "removed" or first.get("cleanup_pid_status") != "dead":
        return False, f"cleanup payload missing removed/dead status: {summary}"
    return True, "ok"


def _make_stage5_assignment_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE operation_table_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_case_id INTEGER NOT NULL,
            table_code TEXT NOT NULL,
            assigned_at TEXT NOT NULL,
            released_at TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_by_role TEXT NOT NULL DEFAULT 'operblock',
            created_by_client_id TEXT,
            revision INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'now')),
            last_modified_by TEXT
        );
        CREATE UNIQUE INDEX idx_operation_assignments_one_active_per_table
        ON operation_table_assignments(table_code)
        WHERE status = 'active' AND released_at IS NULL;
        """
    )
    from rem_card.app.operblock_offline_store import _ensure_shadow_map_table

    _ensure_shadow_map_table(conn)
    return conn


def _stage5_assignment_source(
    *,
    remote_id: int,
    operation_case_id: int,
    table_code: str = "emergency",
    assigned_at: str = "2026-06-27 08:00:00",
    status: str = "active",
) -> dict[str, Any]:
    return {
        "id": int(remote_id),
        "operation_case_id": int(operation_case_id),
        "table_code": table_code,
        "assigned_at": assigned_at,
        "released_at": None,
        "status": status,
        "created_by_role": "operblock",
        "created_by_client_id": "network-client",
        "revision": 1,
        "updated_at": assigned_at,
        "last_modified_by": "operblock",
    }


def _capture_operblock_offline_store_metrics():
    import rem_card.app.operblock_offline_store as store

    events: list[dict[str, Any]] = []
    original_record_metric = store.record_metric

    def fake_record_metric(name, value=None, **fields):
        payload = {"metric": str(name), "value": value}
        payload.update(fields)
        events.append(payload)

    store.record_metric = fake_record_metric
    return store, events, original_record_metric


def _restore_operblock_offline_store_metrics(store, original_record_metric) -> None:
    store.record_metric = original_record_metric


def _assignment_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT id, operation_case_id, table_code, assigned_at, released_at, status
            FROM operation_table_assignments
            ORDER BY id
            """
        ).fetchall()
    ]


def _check_opblock_shadow_mirror_duplicate_table_code_idempotent(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    store, events, original_record_metric = _capture_operblock_offline_store_metrics()
    conn = _make_stage5_assignment_conn()
    try:
        conn.execute(
            """
            INSERT INTO operation_table_assignments (
                operation_case_id, table_code, assigned_at, status, created_by_role, created_by_client_id, last_modified_by
            ) VALUES (12, 'emergency', '2026-06-27 08:00:00', 'active', 'operblock', 'old-client', 'operblock')
            """
        )
        local_id = store._mirror_operation_table_assignment(
            conn,
            source=_stage5_assignment_source(remote_id=100, operation_case_id=12),
            overrides={"operation_case_id": 12},
            offline_case_uuid="case-12",
            remote_id=100,
            reason="stage5_duplicate",
        )
        rows = _assignment_rows(conn)
        if len(rows) != 1 or rows[0]["id"] != local_id or rows[0]["operation_case_id"] != 12:
            return False, f"duplicate same table_code did not stay idempotent: {rows}"
        if any("UNIQUE constraint failed" in str(event) for event in events):
            return False, f"duplicate assignment leaked UNIQUE error: {events}"
        if not any(event.get("metric") == "opblock_shadow_mirror_assignment_upserted" for event in events):
            return False, f"assignment upsert metric missing: {events}"
        return True, "ok"
    finally:
        conn.close()
        _restore_operblock_offline_store_metrics(store, original_record_metric)


def _check_opblock_shadow_mirror_table_code_reassignment_updates_row(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    store, events, original_record_metric = _capture_operblock_offline_store_metrics()
    conn = _make_stage5_assignment_conn()
    try:
        conn.execute(
            """
            INSERT INTO operation_table_assignments (
                operation_case_id, table_code, assigned_at, status, created_by_role, created_by_client_id, last_modified_by
            ) VALUES (12, 'emergency', '2026-06-27 08:00:00', 'active', 'operblock', 'old-client', 'operblock')
            """
        )
        store._mirror_operation_table_assignment(
            conn,
            source=_stage5_assignment_source(remote_id=101, operation_case_id=13, assigned_at="2026-06-27 09:00:00"),
            overrides={"operation_case_id": 13},
            offline_case_uuid="case-13",
            remote_id=101,
            reason="stage5_reassignment",
        )
        rows = _assignment_rows(conn)
        active_rows = [row for row in rows if row["status"] == "active" and row["released_at"] is None]
        if len(rows) != 1 or len(active_rows) != 1 or active_rows[0]["operation_case_id"] != 13:
            return False, f"table_code reassignment did not update one active row: {rows}"
        resolved = [
            event for event in events
            if event.get("metric") == "opblock_shadow_mirror_duplicate_assignment_resolved"
        ]
        if not resolved or resolved[-1].get("old_operation_case_id") != 12 or resolved[-1].get("new_operation_case_id") != 13:
            return False, f"duplicate assignment resolution metric missing/mismatched: {events}"
        return True, "ok"
    finally:
        conn.close()
        _restore_operblock_offline_store_metrics(store, original_record_metric)


def _check_opblock_shadow_mirror_stale_assignment_removed_or_deactivated(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    store, events, original_record_metric = _capture_operblock_offline_store_metrics()
    conn = _make_stage5_assignment_conn()
    try:
        conn.execute(
            """
            INSERT INTO operation_table_assignments (
                id, operation_case_id, table_code, assigned_at, status, created_by_role, created_by_client_id, last_modified_by
            ) VALUES (1, 12, 'emergency', '2026-06-27 08:00:00', 'active', 'operblock', 'old-client', 'operblock')
            """
        )
        store._remember_mapping(conn, "case-12", "operation_table_assignments", 100, 1)
        deactivated = store._deactivate_stale_shadow_assignments_for_case(
            conn,
            local_case_id=12,
            offline_case_uuid="case-12",
            active_remote_assignment_ids=set(),
            source="stage5_stale",
        )
        rows = _assignment_rows(conn)
        if deactivated != 1 or rows[0]["status"] != "released" or rows[0]["released_at"] is None:
            return False, f"stale assignment was not deactivated: deactivated={deactivated}, rows={rows}"
        if not any(event.get("metric") == "opblock_shadow_mirror_assignment_stale_deactivated" for event in events):
            return False, f"stale deactivation metric missing: {events}"
        return True, "ok"
    finally:
        conn.close()
        _restore_operblock_offline_store_metrics(store, original_record_metric)


def _check_opblock_shadow_mirror_repeat_run_is_noop(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    store, events, original_record_metric = _capture_operblock_offline_store_metrics()
    conn = _make_stage5_assignment_conn()
    try:
        payload = _stage5_assignment_source(remote_id=100, operation_case_id=12)
        first_id = store._mirror_operation_table_assignment(
            conn,
            source=payload,
            overrides={"operation_case_id": 12},
            offline_case_uuid="case-12",
            remote_id=100,
            reason="stage5_repeat",
        )
        first_rows = _assignment_rows(conn)
        second_id = store._mirror_operation_table_assignment(
            conn,
            source=payload,
            overrides={"operation_case_id": 12},
            offline_case_uuid="case-12",
            remote_id=100,
            reason="stage5_repeat",
        )
        second_rows = _assignment_rows(conn)
        if first_id != second_id or len(second_rows) != 1 or first_rows != second_rows:
            return False, f"repeat mirror was not stable: first={first_rows}, second={second_rows}"
        if not any(event.get("metric") == "opblock_shadow_mirror_assignment_upserted" and event.get("action") == "noop" for event in events):
            return False, f"repeat mirror did not record noop upsert: {events}"
        return True, "ok"
    finally:
        conn.close()
        _restore_operblock_offline_store_metrics(store, original_record_metric)


def _check_opblock_shadow_mirror_no_unique_constraint_in_assignment_path(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    source = (PROJECT_ROOT / "app/operblock_offline_store.py").read_text(encoding="utf-8")
    mirror_start = source.find("def _mirror_active_case")
    mirror_end = source.find("def mirror_active_operblock_cases_from_network_db", mirror_start)
    mirror_source = source[mirror_start:mirror_end]
    if "_mirror_operation_table_assignment(" not in mirror_source:
        return False, "active case mirror no longer routes assignments through idempotent helper"
    if 'table_name="operation_table_assignments"' in mirror_source:
        return False, "active case mirror still uses generic blind mapped insert for assignments"
    required = (
        "_active_assignment_for_table",
        "_forget_assignment_mappings_for_local_id",
        "opblock_shadow_mirror_duplicate_assignment_resolved",
        "opblock_shadow_mirror_assignment_upserted",
    )
    missing = [token for token in required if token not in source]
    if missing:
        return False, f"idempotent assignment helper missing conflict handling tokens: {missing}"
    return True, "ok"


def _check_stage5_does_not_change_network_commit_result(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    source = (PROJECT_ROOT / "services/data_service.py").read_text(encoding="utf-8")
    if "mirror_active_operblock_cases_from_network_db(self.db" not in source:
        return False, "Stage 5 changed shadow mirror call site unexpectedly"
    if "opblock_shadow_mirror_finished" not in source or "opblock_shadow_mirror_failed" not in source:
        return False, "shadow mirror diagnostics were removed from DataService"
    return True, "ok"


def _check_stage5_does_not_change_sqlite_profile(temp_root: str) -> tuple[bool, str]:
    return _check_opblock_stage1_no_sqlite_profile_changes(temp_root)


def _check_stage5_does_not_enable_recovery_on_locked_busy(temp_root: str) -> tuple[bool, str]:
    return _check_stage4_does_not_enable_recovery_on_locked_busy(temp_root)


def _check_analyzer_understands_shadow_mirror_assignment_conflict(temp_root: str) -> tuple[bool, str]:
    logs_dir = Path(temp_root, "opblock_stage5_shadow_logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = logs_dir / "metrics_20260626.jsonl"
    rows = [
        {
            "ts": "2026-06-26T11:14:02+10:00",
            "metric": "user_return_from_idle",
            "idle_ms": 530000,
            "first_action": "operblock_update_operation_case_form_data",
        },
        {
            "ts": "2026-06-26T11:14:03+10:00",
            "metric": "opblock_shadow_mirror_failed",
            "error_message_sanitized": "UNIQUE constraint failed: operation_table_assignments.table_code",
        },
        {
            "ts": "2026-06-26T11:18:02+10:00",
            "metric": "user_return_from_idle",
            "idle_ms": 540000,
            "first_action": "operblock_update_operation_case_form_data",
        },
        {
            "ts": "2026-06-26T11:18:03+10:00",
            "metric": "opblock_shadow_mirror_duplicate_assignment_resolved",
            "table_code": "emergency",
            "old_operation_case_id": 12,
            "new_operation_case_id": 13,
        },
        {
            "ts": "2026-06-26T11:18:03.100000+10:00",
            "metric": "opblock_shadow_mirror_assignment_upserted",
            "table_code": "emergency",
            "operation_case_id": 13,
            "previous_operation_case_id": 12,
            "action": "update",
        },
    ]
    metrics_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "analyze_opblock_idle_stalls.py"),
            "--logs",
            str(logs_dir),
            "--json",
        ],
        cwd=str(PROJECT_ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    if result.returncode != 0:
        return False, f"Stage 5 analyzer failed: {result.stderr[-500:]}"
    summary = json.loads(result.stdout)
    classifications = [incident.get("classification") for incident in (summary.get("incidents") or [])]
    if "shadow_mirror_unique_constraint_failed" not in classifications:
        return False, f"unique constraint classification missing: {summary}"
    if "mirror_duplicate_resolved" not in classifications:
        return False, f"resolved duplicate classification missing: {summary}"
    return True, "ok"


class _Stage6OperblockRuntimeContext:
    mode = "network"


class _Stage6OperblockFakeDb:
    def __init__(self):
        self.runtime_context = _Stage6OperblockRuntimeContext()
        self.db_path = os.path.abspath("stage6_network.db")
        self.write_calls = 0

    def run_write_operation(self, operation, source: str = ""):
        self.write_calls += 1
        return operation()

    def fetch_all_remcard(self, query: str, params: tuple = ()):
        return []

    def get_data_version(self) -> int:
        return 0

    def get_latest_change_id(self, admission_id=None, include_global: bool = True) -> int:
        return 0

    def fetch_changes_since(self, last_change_id: int, admission_id=None, include_global: bool = True):
        return []

    def get_changed_entities_since(self, last_change_id: int, admission_id=None, include_global: bool = True):
        return set()


class _Stage6ImmediateQueue:
    def __init__(self, events: list[tuple], *, auto_run_mirror: bool = False):
        self.events = events
        self.auto_run_mirror = bool(auto_run_mirror)
        self.mirror_tasks: list[tuple] = []
        self._active_count = 0
        self._accepting = True

    def submit(
        self,
        func,
        description: str,
        on_success=None,
        on_error=None,
        retryable: bool = True,
        retries_left: int = 10,
    ):
        _ = retries_left
        if not self._accepting:
            raise RuntimeError("fake queue closed")
        self.events.append(("queue_submit", str(description or ""), bool(retryable)))
        if str(description or "").startswith("opblock_shadow_mirror"):
            if not self.auto_run_mirror:
                self.mirror_tasks.append((func, str(description or ""), bool(retryable)))
                return
            return self._run_task(func, description, on_success=on_success, on_error=on_error)
        return self._run_task(func, description, on_success=on_success, on_error=on_error)

    def _run_task(self, func, description: str, *, on_success=None, on_error=None):
        self._active_count += 1
        try:
            result = func()
        except Exception as exc:
            if on_error:
                on_error(exc)
                return None
            raise
        finally:
            self._active_count = max(0, self._active_count - 1)
        if on_success:
            on_success(result)
        return result

    def run_mirror_tasks(self):
        while self.mirror_tasks:
            func, description, _retryable = self.mirror_tasks.pop(0)
            self._run_task(func, description)

    def is_idle(self) -> bool:
        return self._active_count <= 0 and not self.mirror_tasks

    def pending_count(self) -> int:
        return len(self.mirror_tasks)

    def active_count(self) -> int:
        return int(self._active_count)

    def is_accepting(self) -> bool:
        return bool(self._accepting)

    def shutdown(self, timeout: float = 1.0) -> bool:
        _ = timeout
        self._accepting = False
        return True


def _make_stage6_data_service(events: list[tuple], *, auto_run_mirror: bool = False):
    from rem_card.services.data_service import DataService

    service = DataService(_Stage6OperblockFakeDb())
    service.set_runtime_role("operblock")
    service.stop_data_update_monitor(timeout=1.0)
    service._queue.shutdown(timeout=1.0)
    service._queue = _Stage6ImmediateQueue(events, auto_run_mirror=auto_run_mirror)
    service._record_operblock_write_intent = lambda description: "stage6-operation-uuid"
    service._mark_operblock_write_failed = lambda operation_uuid, description, exc: events.append(("mark_failed", str(exc)))
    return service


def _connect_stage6_data_service_events(service, events: list[tuple]) -> None:
    from PySide6.QtCore import Qt

    service.write_finished.connect(lambda description: events.append(("write_finished", str(description or ""))), Qt.DirectConnection)
    service.write_failed.connect(lambda message: events.append(("write_failed", str(message or ""))), Qt.DirectConnection)
    service._success_callback_requested.connect(
        lambda callback, result: events.append(("success_signal", result)),
        Qt.DirectConnection,
    )
    service._error_callback_requested.connect(
        lambda callback, exc: events.append(("error_signal", type(exc).__name__, str(exc))),
        Qt.DirectConnection,
    )


def _capture_stage6_data_service_metrics():
    import rem_card.services.data_service as data_service_module

    events: list[dict[str, Any]] = []
    original_record_metric = data_service_module.record_metric

    def fake_record_metric(name, value=None, **fields):
        payload = {"metric": str(name), "value": value}
        payload.update(fields)
        events.append(payload)

    data_service_module.record_metric = fake_record_metric
    return data_service_module, events, original_record_metric


def _restore_stage6_data_service_metrics(data_service_module, original_record_metric) -> None:
    data_service_module.record_metric = original_record_metric


def _patch_stage6_shadow_mirror(*, fail_count: int | None = None):
    import rem_card.app.operblock_offline_store as store

    attempts = {"count": 0}
    original_mirror = store.mirror_active_operblock_cases_from_network_db
    original_mark = store.mark_operblock_write_remote_committed

    def fake_mirror(db_manager, reason: str = ""):
        _ = db_manager, reason
        attempts["count"] += 1
        if fail_count is None or attempts["count"] <= int(fail_count):
            raise RuntimeError(f"stage6 mirror failure {attempts['count']}")
        return None

    store.mirror_active_operblock_cases_from_network_db = fake_mirror
    store.mark_operblock_write_remote_committed = lambda db_manager, **kwargs: None
    return store, attempts, original_mirror, original_mark


def _restore_stage6_shadow_mirror(store, original_mirror, original_mark) -> None:
    store.mirror_active_operblock_cases_from_network_db = original_mirror
    store.mark_operblock_write_remote_committed = original_mark


def _check_shadow_mirror_failure_does_not_fail_committed_network_write(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    events: list[tuple] = []
    service = _make_stage6_data_service(events, auto_run_mirror=False)
    _connect_stage6_data_service_events(service, events)
    metrics_module, metrics, original_metric = _capture_stage6_data_service_metrics()
    store, _attempts, original_mirror, original_mark = _patch_stage6_shadow_mirror(fail_count=None)
    try:
        service._schedule_opblock_shadow_mirror_retry = lambda *args, **kwargs: events.append(("retry_scheduled",))
        accepted = service.enqueue_write(
            "operblock_update_operation_case_form_data:42",
            lambda: events.append(("network_operation",)) or "committed",
            on_success=lambda result: events.append(("ui_success", result)),
            on_error=lambda exc: events.append(("ui_error", str(exc))),
            write_metadata={"operation_case_id": 42, "table_code": "planned", "request_id": "stage6-request"},
        )
        if not accepted or ("network_operation",) not in events:
            return False, f"network write was not accepted/committed: accepted={accepted}, events={events}"
        queue = service._queue
        if not queue.mirror_tasks:
            return False, f"post-commit mirror task was not queued: {events}"
        queue.run_mirror_tasks()
        if any(event[0] in {"write_failed", "error_signal", "ui_error", "mark_failed"} for event in events):
            return False, f"mirror failure leaked into write failure path: {events}"
        if service.is_network_outage_detected():
            return False, "mirror failure triggered runtime outage"
        metric_names = {event.get("metric") for event in metrics}
        required = {
            "opblock_shadow_mirror_post_commit_failed",
            "opblock_shadow_mirror_failure_did_not_fail_network_write",
        }
        missing = sorted(required - metric_names)
        if missing:
            return False, f"mirror failure metrics missing: {missing}; metrics={metrics}"
        return True, "ok"
    finally:
        _restore_stage6_shadow_mirror(store, original_mirror, original_mark)
        _restore_stage6_data_service_metrics(metrics_module, original_metric)
        service.shutdown()


def _check_shadow_mirror_runs_after_network_commit(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    events: list[tuple] = []
    service = _make_stage6_data_service(events, auto_run_mirror=False)
    _connect_stage6_data_service_events(service, events)
    metrics_module, _metrics, original_metric = _capture_stage6_data_service_metrics()
    try:
        service.enqueue_write(
            "operblock_update_operation_case_form_data:7",
            lambda: events.append(("network_operation",)) or "committed",
            on_success=lambda result: events.append(("ui_success", result)),
            write_metadata={"operation_case_id": 7, "table_code": "emergency", "request_id": "stage6-order"},
        )
        names = [event[0] for event in events]
        if "network_operation" not in names or "success_signal" not in names:
            return False, f"network/success events missing: {events}"
        mirror_submit_index = next(
            (idx for idx, event in enumerate(events) if event[0] == "queue_submit" and event[1] == "opblock_shadow_mirror_post_commit"),
            -1,
        )
        if mirror_submit_index < 0:
            return False, f"mirror task was not queued: {events}"
        if names.index("network_operation") > mirror_submit_index:
            return False, f"mirror was queued before network operation completed: {events}"
        if names.index("success_signal") > mirror_submit_index:
            return False, f"user-visible success was not posted before mirror task: {events}"
        return True, "ok"
    finally:
        _restore_stage6_data_service_metrics(metrics_module, original_metric)
        service.shutdown()


def _check_shadow_mirror_failure_does_not_trigger_recovery(temp_root: str) -> tuple[bool, str]:
    ok, details = _check_shadow_mirror_failure_does_not_fail_committed_network_write(temp_root)
    if not ok:
        return ok, details
    data_service_source = (PROJECT_ROOT / "services" / "data_service.py").read_text(encoding="utf-8")
    failure_idx = data_service_source.find("def _record_opblock_shadow_mirror_failure(")
    enqueue_idx = data_service_source.find("def enqueue_write(", failure_idx)
    failure_source = data_service_source[failure_idx:enqueue_idx]
    if "_handle_database_access_failure" in failure_source or "write_failed.emit" in failure_source or "_error_callback_requested.emit" in failure_source:
        return False, "shadow mirror failure path is wired to recovery/write error callback"
    return True, "ok"


def _check_shadow_mirror_retry_is_bounded(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    events: list[tuple] = []
    service = _make_stage6_data_service(events, auto_run_mirror=True)
    metrics_module, metrics, original_metric = _capture_stage6_data_service_metrics()
    store, attempts, original_mirror, original_mark = _patch_stage6_shadow_mirror(fail_count=None)
    original_timer = metrics_module.threading.Timer
    original_delays = metrics_module.OPBLOCK_SHADOW_MIRROR_RETRY_DELAYS_MS

    class ImmediateTimer:
        def __init__(self, delay, callback):
            self.delay = delay
            self.callback = callback
            self.daemon = False

        def start(self):
            events.append(("timer", self.delay))
            self.callback()

    try:
        metrics_module.threading.Timer = ImmediateTimer
        metrics_module.OPBLOCK_SHADOW_MIRROR_RETRY_DELAYS_MS = (1, 1)
        service._mirror_operblock_write_after_commit(
            "operblock_update_operation_case_form_data:9",
            operation_uuid="stage6-bounded",
            context={"operation_case_id": 9, "admission_id": None, "table_code": "planned", "request_id": "stage6-bounded"},
        )
        if attempts["count"] != 3:
            return False, f"mirror retry attempts must be bounded at 3, got {attempts['count']}; events={events}"
        metric_names = [event.get("metric") for event in metrics]
        if metric_names.count("opblock_shadow_mirror_retry_scheduled") != 2:
            return False, f"expected exactly two scheduled retries: {metrics}"
        if "opblock_shadow_mirror_retry_exhausted" not in metric_names:
            return False, f"retry exhausted metric missing: {metrics}"
        if any(event[0] == "queue_submit" and event[1] != "opblock_shadow_mirror_post_commit" and event[1] != "opblock_shadow_mirror_retry" for event in events):
            return False, f"unexpected queue task during mirror retry: {events}"
        return True, "ok"
    finally:
        metrics_module.threading.Timer = original_timer
        metrics_module.OPBLOCK_SHADOW_MIRROR_RETRY_DELAYS_MS = original_delays
        _restore_stage6_shadow_mirror(store, original_mirror, original_mark)
        _restore_stage6_data_service_metrics(metrics_module, original_metric)
        service.shutdown()


def _check_shadow_mirror_success_after_retry(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    events: list[tuple] = []
    service = _make_stage6_data_service(events, auto_run_mirror=True)
    metrics_module, metrics, original_metric = _capture_stage6_data_service_metrics()
    store, attempts, original_mirror, original_mark = _patch_stage6_shadow_mirror(fail_count=1)
    original_timer = metrics_module.threading.Timer
    original_delays = metrics_module.OPBLOCK_SHADOW_MIRROR_RETRY_DELAYS_MS

    class ImmediateTimer:
        def __init__(self, delay, callback):
            self.delay = delay
            self.callback = callback
            self.daemon = False

        def start(self):
            events.append(("timer", self.delay))
            self.callback()

    try:
        metrics_module.threading.Timer = ImmediateTimer
        metrics_module.OPBLOCK_SHADOW_MIRROR_RETRY_DELAYS_MS = (1, 1)
        service._mirror_operblock_write_after_commit(
            "operblock_update_operation_case_form_data:10",
            operation_uuid="stage6-retry-success",
            context={"operation_case_id": 10, "admission_id": None, "table_code": "emergency", "request_id": "stage6-retry-success"},
        )
        if attempts["count"] != 2:
            return False, f"mirror should succeed on second attempt, attempts={attempts['count']}; events={events}"
        metric_names = {event.get("metric") for event in metrics}
        if "opblock_shadow_mirror_retry_succeeded" not in metric_names:
            return False, f"retry success metric missing: {metrics}"
        if "opblock_shadow_mirror_retry_exhausted" in metric_names:
            return False, f"retry exhausted after successful retry: {metrics}"
        return True, "ok"
    finally:
        metrics_module.threading.Timer = original_timer
        metrics_module.OPBLOCK_SHADOW_MIRROR_RETRY_DELAYS_MS = original_delays
        _restore_stage6_shadow_mirror(store, original_mirror, original_mark)
        _restore_stage6_data_service_metrics(metrics_module, original_metric)
        service.shutdown()


def _check_user_pending_cleared_after_network_commit_even_if_mirror_failed(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    events: list[tuple] = []
    service = _make_stage6_data_service(events, auto_run_mirror=False)
    _connect_stage6_data_service_events(service, events)
    metrics_module, _metrics, original_metric = _capture_stage6_data_service_metrics()
    store, _attempts, original_mirror, original_mark = _patch_stage6_shadow_mirror(fail_count=None)
    try:
        service._schedule_opblock_shadow_mirror_retry = lambda *args, **kwargs: None
        service.enqueue_write(
            "operblock_release_operation_table:11",
            lambda: events.append(("network_operation",)) or "committed",
            on_success=lambda result: events.append(("ui_success", result)),
            on_error=lambda exc: events.append(("ui_error", str(exc))),
            write_metadata={"operation_case_id": 11, "table_code": "planned", "request_id": "stage6-pending"},
        )
        names = [event[0] for event in events]
        if "success_signal" not in names:
            return False, f"success signal was not posted after network commit: {events}"
        queue = service._queue
        queue.run_mirror_tasks()
        if any(event[0] in {"error_signal", "ui_error", "write_failed"} for event in events):
            return False, f"mirror failure changed user-visible pending/error state: {events}"
        mirror_submit_index = next(
            (idx for idx, event in enumerate(events) if event[0] == "queue_submit" and event[1] == "opblock_shadow_mirror_post_commit"),
            -1,
        )
        if names.index("success_signal") > mirror_submit_index:
            return False, f"success was not posted before mirror task: {events}"
        return True, "ok"
    finally:
        _restore_stage6_shadow_mirror(store, original_mirror, original_mark)
        _restore_stage6_data_service_metrics(metrics_module, original_metric)
        service.shutdown()


def _check_stage5_idempotency_still_holds(temp_root: str) -> tuple[bool, str]:
    checks = (
        _check_opblock_shadow_mirror_duplicate_table_code_idempotent,
        _check_opblock_shadow_mirror_table_code_reassignment_updates_row,
        _check_opblock_shadow_mirror_repeat_run_is_noop,
    )
    for check in checks:
        ok, details = check(temp_root)
        if not ok:
            return False, f"{check.__name__}: {details}"
    return True, "ok"


def _check_stage6_shadow_mirror_safety_invariants(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    data_service_source = (PROJECT_ROOT / "services" / "data_service.py").read_text(encoding="utf-8")
    required = {
        "opblock_shadow_mirror_post_commit_started",
        "opblock_shadow_mirror_post_commit_succeeded",
        "opblock_shadow_mirror_post_commit_failed",
        "opblock_shadow_mirror_retry_scheduled",
        "opblock_shadow_mirror_retry_succeeded",
        "opblock_shadow_mirror_retry_exhausted",
        "opblock_shadow_mirror_decoupled_from_write",
        "opblock_shadow_mirror_failure_did_not_fail_network_write",
        "retryable=False",
        "OPBLOCK_SHADOW_MIRROR_MAX_ATTEMPTS = 3",
    }
    missing = sorted(token for token in required if token not in data_service_source)
    if missing:
        return False, f"Stage 6 safety token(s) missing: {missing}"
    if "local-first" in data_service_source.lower() or "outbox" in data_service_source.lower() or "command_queue" in data_service_source.lower():
        return False, "Stage 6 must not introduce persistent local-first/outbox/command queue wording into DataService"
    ok, details = _check_opblock_stage1_no_sqlite_profile_changes(temp_root)
    if not ok:
        return ok, details
    return True, "ok"


def _check_analyzer_understands_shadow_mirror_post_commit_retry(temp_root: str) -> tuple[bool, str]:
    logs_dir = Path(temp_root, "opblock_stage6_shadow_logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = logs_dir / "metrics_20260627.jsonl"
    rows = [
        {
            "ts": "2026-06-27T09:00:00+10:00",
            "metric": "user_return_from_idle",
            "idle_ms": 520000,
            "first_action": "operblock_update_operation_case_form_data",
        },
        {
            "ts": "2026-06-27T09:00:01+10:00",
            "metric": "opblock_shadow_mirror_post_commit_failed",
            "operation_name": "operblock_update_operation_case_form_data:31",
            "operation_case_id": 31,
            "table_code": "planned",
            "attempt": 1,
            "error_class": "OperationalError",
            "error_message_sanitized": "database is locked",
        },
        {
            "ts": "2026-06-27T09:05:00+10:00",
            "metric": "user_return_from_idle",
            "idle_ms": 530000,
            "first_action": "operblock_release_operation_table",
        },
        {
            "ts": "2026-06-27T09:05:01+10:00",
            "metric": "opblock_shadow_mirror_retry_exhausted",
            "operation_name": "operblock_release_operation_table:32",
            "operation_case_id": 32,
            "table_code": "emergency",
            "attempt": 3,
            "error_class": "OperationalError",
            "error_message_sanitized": "database is locked",
        },
        {
            "ts": "2026-06-27T09:10:00+10:00",
            "metric": "user_return_from_idle",
            "idle_ms": 540000,
            "first_action": "operblock_update_operation_case_form_data",
        },
        {
            "ts": "2026-06-27T09:10:01+10:00",
            "metric": "opblock_action_finished",
            "action": "operblock_update_operation_case_form_data",
            "result": "error",
            "error_class": "OperationalError",
            "error_message_sanitized": "unable to open database file",
        },
    ]
    metrics_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "analyze_opblock_idle_stalls.py"),
            "--logs",
            str(logs_dir),
            "--json",
        ],
        cwd=str(PROJECT_ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    if result.returncode != 0:
        return False, f"Stage 6 analyzer failed: {result.stderr[-500:]}"
    summary = json.loads(result.stdout)
    classifications = [incident.get("classification") for incident in (summary.get("incidents") or [])]
    required = {"mirror_failed_after_commit", "mirror_retry_exhausted", "network_write_failed"}
    missing = sorted(required - set(classifications))
    if missing:
        return False, f"Stage 6 analyzer classifications missing {missing}: {summary}"
    return True, "ok"
