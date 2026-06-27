#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


TS_RE = re.compile(r"(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[,.]\d{1,6})?)")
KEY_VALUE_RE = re.compile(r"(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)=(?P<value>[^,\s]+)")
OPBLOCK_EVENTS = {
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
    "sqlite_write_lock_local_dead_pid_detected",
    "sqlite_write_lock_stale_removed",
    "sqlite_write_lock_stale_cleanup_skipped",
    "sqlite_write_lock_stale_cleanup_failed",
    "opblock_shadow_mirror_started",
    "opblock_shadow_mirror_finished",
    "opblock_shadow_mirror_failed",
    "opblock_shadow_mirror_deferred_for_foreground_resume",
    "opblock_shadow_mirror_assignment_upserted",
    "opblock_shadow_mirror_assignment_stale_deactivated",
    "opblock_shadow_mirror_assignment_stale_removed",
    "opblock_shadow_mirror_duplicate_assignment_resolved",
    "foreground_resume_lease_started",
    "foreground_resume_lease_finished",
    "maintenance_deferred_for_foreground_resume",
    "maintenance_resume_after_foreground",
    "maintenance_deferral_count",
    "maintenance_deferral_max_age_ms",
    "maintenance_starvation_prevented",
    "maintenance_resumed_after_idle",
    "maintenance_overlap_observed",
    "ui_pending_state_observed",
    "ui_pending_cleared_after_busy_timeout",
    "event_loop_pause_ms",
}


@dataclass
class Event:
    ts: datetime | None
    kind: str
    value: float | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    source: str = ""


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze opblock idle/resume stall diagnostics.")
    parser.add_argument("--logs", required=True, help="Log directory or a single log/metrics file.")
    parser.add_argument("--date", help="Filter by date: YYYY-MM-DD or YYYYMMDD.")
    parser.add_argument("--window-sec", type=float, default=90.0, help="Correlation window around return from idle.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser.parse_args(argv)


def _parse_ts(raw: Any) -> datetime | None:
    if raw is None:
        return None
    text = str(raw).strip().replace(",", ".")
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        match = TS_RE.search(text)
        if not match:
            return None
        return _parse_ts(match.group("ts"))


def _float_value(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iter_input_files(path: Path) -> Iterable[Path]:
    if path.is_file():
        yield path
        return
    for pattern in ("metrics_*.jsonl", "*.log", "*.txt", "fault*.jsonl", "crash*.log"):
        yield from sorted(path.rglob(pattern))


def _date_matches(path: Path, date_filter: str | None) -> bool:
    if not date_filter:
        return True
    normalized = date_filter.replace("-", "")
    return normalized in path.name.replace("-", "")


def _parse_json_metric(line: str, source: str) -> Event | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    kind = str(payload.get("metric") or payload.get("event") or "")
    if kind not in OPBLOCK_EVENTS:
        return None
    return Event(_parse_ts(payload.get("ts") or payload.get("timestamp")), kind, _float_value(payload.get("value")), payload, source)


def _parse_text_event(line: str, source: str) -> Event | None:
    if "[UIWatchdog]" not in line and not any(name in line for name in OPBLOCK_EVENTS):
        return None
    fields = {match.group("key"): match.group("value") for match in KEY_VALUE_RE.finditer(line)}
    kind = "event_loop_pause_ms" if "event_loop_pause_ms" in fields else ""
    if not kind:
        kind = next((name for name in OPBLOCK_EVENTS if name in line), "")
    if not kind:
        return None
    return Event(_parse_ts(line), kind, _float_value(fields.get("event_loop_pause_ms") or fields.get("value")), fields, source)


def _load_events(logs: Path, date_filter: str | None) -> list[Event]:
    events: list[Event] = []
    for path in _iter_input_files(logs):
        if not _date_matches(path, date_filter):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            event = _parse_json_metric(line, str(path)) or _parse_text_event(line, str(path))
            if event is not None:
                events.append(event)
    events.sort(key=lambda item: item.ts or datetime.min)
    return events


def _nearby(events: list[Event], center: datetime, window_sec: float) -> list[Event]:
    window = max(0.0, float(window_sec or 0.0))
    result: list[Event] = []
    for event in events:
        if event.ts is None:
            continue
        if abs((event.ts - center).total_seconds()) <= window:
            result.append(event)
    return result


def _first(events: list[Event], *kinds: str) -> Event | None:
    wanted = set(kinds)
    return next((event for event in events if event.kind in wanted), None)


def _classify(related: list[Event]) -> str:
    if _first(related, "sqlite_write_lock_stale_removed"):
        return "local_dead_pid_lock_removed"
    if _first(related, "sqlite_write_lock_stale_cleanup_failed"):
        return "local_dead_pid_cleanup_failed"
    skipped = _first(related, "sqlite_write_lock_stale_cleanup_skipped")
    if skipped:
        reason = str(skipped.fields.get("reason") or "")
        if reason == "other_host":
            return "other_host_lock_wait"
        if reason == "pid_alive":
            return "live_pid_lock_wait"
        if reason in {"unreadable", "parse_error", "permission_denied"}:
            return "unreadable_lock_wait"
        return "local_dead_pid_cleanup_skipped"
    timeout = _first(related, "sqlite_write_lock_timeout")
    finished = _first(related, "opblock_action_finished")
    if timeout:
        phase = str(timeout.fields.get("phase") or "")
        if phase in {"file_lock_timeout", "begin_immediate_timeout"}:
            return phase
        if finished and str(finished.fields.get("result") or "") == "busy_timeout":
            return "opblock_busy_timeout"
        if str(timeout.fields.get("sqlite_error_class") or "") or str(timeout.fields.get("sqlite_error_message_sanitized") or ""):
            return "unknown_busy"
        return "sqlite_write_lock_timeout"
    if _first(related, "sqlite_write_lock_wait_retry", "sqlite_write_lock_wait_started"):
        return "sqlite_write_lock_wait"
    if _first(related, "maintenance_overlap_observed"):
        return "maintenance_overlap"
    if _first(related, "opblock_shadow_mirror_duplicate_assignment_resolved"):
        return "shadow_mirror_duplicate_assignment_resolved"
    if _first(related, "opblock_shadow_mirror_assignment_upserted"):
        return "shadow_mirror_assignment_upsert"
    if _first(related, "opblock_shadow_mirror_assignment_stale_removed", "opblock_shadow_mirror_assignment_stale_deactivated"):
        return "shadow_mirror_stale_assignment_removed"
    shadow_failed = _first(related, "opblock_shadow_mirror_failed")
    if shadow_failed:
        error_text = str(shadow_failed.fields.get("error_message_sanitized") or shadow_failed.fields.get("error") or "")
        if "UNIQUE constraint failed: operation_table_assignments.table_code" in error_text:
            return "shadow_mirror_unique_constraint_failed"
        return "shadow_mirror_failure"
    if _first(related, "foreground_resume_lease_started") and _first(
        related,
        "maintenance_deferred_for_foreground_resume",
        "opblock_shadow_mirror_deferred_for_foreground_resume",
    ):
        return "foreground_resume_protected"
    pending = [event for event in related if event.kind == "ui_pending_state_observed"]
    finished = _first(related, "opblock_action_finished")
    if pending and not finished:
        return "ui_pending_stuck"
    if _first(related, "event_loop_pause_ms"):
        return "unknown_ui_pause"
    return "unknown"


def _cleanup_payload(related: list[Event], lock_event: Event | None) -> dict[str, Any]:
    removed = _first(related, "sqlite_write_lock_stale_removed")
    skipped = _first(related, "sqlite_write_lock_stale_cleanup_skipped")
    failed = _first(related, "sqlite_write_lock_stale_cleanup_failed")
    detected = _first(related, "sqlite_write_lock_local_dead_pid_detected")
    cleanup_event = removed or failed or skipped or detected
    cleanup = ""
    reason = ""
    pid_status = ""
    if removed:
        cleanup = "removed"
        reason = str(removed.fields.get("reason") or "")
        pid_status = "dead"
    elif failed:
        cleanup = "failed"
        reason = str(failed.fields.get("reason") or "")
    elif skipped:
        cleanup = "skipped"
        reason = str(skipped.fields.get("reason") or "")
        pid_status = "live" if reason == "pid_alive" else ("unknown" if reason == "pid_unknown" else "")
    elif detected:
        cleanup = "detected"
        pid_status = "dead"
    return {
        "lock_path": (cleanup_event.fields.get("lock_path") if cleanup_event else (lock_event.fields.get("lock_path") if lock_event else "")),
        "cleanup": cleanup,
        "cleanup_reason": reason,
        "cleanup_pid_status": pid_status,
        "cleanup_holder_pid": (cleanup_event.fields.get("holder_pid") if cleanup_event else None),
        "cleanup_holder_host": (cleanup_event.fields.get("holder_host") if cleanup_event else ""),
        "cleanup_holder_source": (cleanup_event.fields.get("holder_source") if cleanup_event else ""),
    }


def _incident_payload(event: Event, related: list[Event]) -> dict[str, Any]:
    action = _first(related, "opblock_action_started") or event
    finished = _first(related, "opblock_action_finished")
    lock_event = _first(related, "sqlite_write_lock_timeout", "sqlite_write_lock_wait_retry", "sqlite_write_lock_acquired")
    ui_pause = _first(related, "event_loop_pause_ms")
    pending_cleared = _first(related, "ui_pending_cleared_after_busy_timeout")
    lease_started = _first(related, "foreground_resume_lease_started")
    lease_finished = _first(related, "foreground_resume_lease_finished")
    deferred_events = [item for item in related if item.kind == "maintenance_deferred_for_foreground_resume"]
    resumed_events = [item for item in related if item.kind == "maintenance_resume_after_foreground"]
    shadow_deferred = _first(related, "opblock_shadow_mirror_deferred_for_foreground_resume")
    shadow_assignment = _first(
        related,
        "opblock_shadow_mirror_duplicate_assignment_resolved",
        "opblock_shadow_mirror_assignment_upserted",
        "opblock_shadow_mirror_assignment_stale_removed",
        "opblock_shadow_mirror_assignment_stale_deactivated",
        "opblock_shadow_mirror_failed",
    )
    return {
        "incident_at": (event.ts.isoformat(sep=" ") if event.ts else ""),
        "idle_ms": event.fields.get("idle_ms"),
        "first_action": event.fields.get("first_action") or action.fields.get("action"),
        "result": (finished.fields.get("result") if finished else _classify(related)),
        "classification": _classify(related),
        "foreground_lease": bool(lease_started),
        "foreground_lease_id": lease_started.fields.get("lease_id") if lease_started else "",
        "foreground_lease_duration_ms": (
            lease_finished.fields.get("duration_ms")
            if lease_finished
            else (lease_started.fields.get("suppress_maintenance_for_ms") if lease_started else None)
        ),
        "deferred_maintenance": [str(item.fields.get("task") or "") for item in deferred_events],
        "resumed_maintenance": [str(item.fields.get("task") or "") for item in resumed_events],
        "wait_ms": (lock_event.fields.get("total_wait_ms") if lock_event else None),
        "sqlite_write_lock": ("timeout" if lock_event and lock_event.kind == "sqlite_write_lock_timeout" else ("wait" if lock_event else "none")),
        "sqlite_lock_phase": (lock_event.fields.get("phase") if lock_event else ""),
        "lock_holder_pid": (lock_event.fields.get("lock_holder_pid") if lock_event else None),
        "lock_holder_host": (lock_event.fields.get("lock_holder_host") if lock_event else ""),
        "lock_holder_source": (lock_event.fields.get("lock_holder_source") if lock_event else ""),
        **_cleanup_payload(related, lock_event),
        "ui_pause_ms": (ui_pause.value if ui_pause else None),
        "ui_result": (finished.fields.get("result") if finished else ("controlled busy" if pending_cleared else "")),
        "pending_cleared_after_busy_timeout": bool(pending_cleared),
        "maintenance_overlap": bool(_first(related, "maintenance_overlap_observed")),
        "shadow_mirror_overlap": bool(_first(related, "opblock_shadow_mirror_started", "opblock_shadow_mirror_failed")),
        "shadow_mirror_deferred": bool(shadow_deferred),
        "shadow_mirror_table_code": (shadow_assignment.fields.get("table_code") if shadow_assignment else ""),
        "shadow_mirror_previous_case": (
            shadow_assignment.fields.get("previous_operation_case_id")
            if shadow_assignment
            else None
        ),
        "shadow_mirror_old_case": (
            shadow_assignment.fields.get("old_operation_case_id")
            if shadow_assignment
            else None
        ),
        "shadow_mirror_new_case": (
            shadow_assignment.fields.get("new_operation_case_id")
            if shadow_assignment
            else (shadow_assignment.fields.get("operation_case_id") if shadow_assignment else None)
        ),
        "shadow_mirror_error": (
            shadow_assignment.fields.get("error_message_sanitized") or shadow_assignment.fields.get("error")
            if shadow_assignment
            else ""
        ),
        "events": [item.kind for item in related],
    }


def _format_ms(value: Any) -> str:
    numeric = _float_value(value)
    if numeric is None:
        return "unknown"
    if numeric >= 60000:
        minutes = int(numeric // 60000)
        seconds = int((numeric % 60000) // 1000)
        return f"{minutes}m {seconds}s"
    return f"{int(numeric)} ms"


def _print_shadow_mirror_text(incident: dict[str, Any]) -> None:
    classification = incident.get("classification")
    if incident.get("shadow_mirror_deferred"):
        print("Shadow mirror: deferred")
        return
    if classification in {
        "shadow_mirror_duplicate_assignment_resolved",
        "shadow_mirror_assignment_upsert",
        "shadow_mirror_stale_assignment_removed",
    }:
        print("Shadow mirror: assignment upserted")
        print(f"Table code: {incident.get('shadow_mirror_table_code') or 'unknown'}")
        print(f"Previous case: {incident.get('shadow_mirror_previous_case') or incident.get('shadow_mirror_old_case') or 'unknown'}")
        print(f"New case: {incident.get('shadow_mirror_new_case') or 'unknown'}")
        return
    if classification == "shadow_mirror_unique_constraint_failed":
        print("Shadow mirror: failed")
        print(f"Error: {incident.get('shadow_mirror_error') or 'UNIQUE constraint failed: operation_table_assignments.table_code'}")
        print(f"Table code: {incident.get('shadow_mirror_table_code') or 'unknown'}")
        return
    print(f"Shadow mirror overlap: {'yes' if incident.get('shadow_mirror_overlap') else 'none'}")


def _conclusion_text(classification: Any) -> str:
    conclusion = str(classification or "")
    if conclusion == "local_dead_pid_lock_removed":
        return "stale local db.lock cleaned safely"
    if conclusion in {"other_host_lock_wait", "live_pid_lock_wait", "unreadable_lock_wait"}:
        return "remote/unknown lock was not removed"
    if conclusion == "local_dead_pid_cleanup_failed":
        return "local dead-PID cleanup failed; lock was not removed"
    if conclusion in {"shadow_mirror_duplicate_assignment_resolved", "shadow_mirror_assignment_upsert"}:
        return "duplicate assignment handled idempotently"
    if conclusion == "shadow_mirror_stale_assignment_removed":
        return "stale shadow table assignment removed"
    if conclusion == "shadow_mirror_unique_constraint_failed":
        return "shadow mirror duplicate table assignment conflict"
    if conclusion in {"file_lock_timeout", "begin_immediate_timeout", "opblock_busy_timeout", "sqlite_write_lock_timeout"}:
        return "interactive opblock write timed out instead of hanging"
    return conclusion


def _print_text(incidents: list[dict[str, Any]]) -> None:
    if not incidents:
        print("No opblock idle/resume incidents found.")
        return
    for incident in incidents:
        print(f"Incident: {incident['incident_at']}")
        print(f"Idle before action: {_format_ms(incident.get('idle_ms'))}")
        print(f"First action: {incident.get('first_action') or 'unknown'}")
        if incident.get("foreground_lease"):
            print(f"Foreground lease: started, duration={_format_ms(incident.get('foreground_lease_duration_ms'))}")
        else:
            print("Foreground lease: none")
        deferred = [task for task in incident.get("deferred_maintenance") or [] if task]
        resumed = [task for task in incident.get("resumed_maintenance") or [] if task]
        print(f"Deferred maintenance: {', '.join(deferred) if deferred else 'none'}")
        print(f"Result: {incident.get('result') or 'unknown'}")
        if incident.get("sqlite_write_lock") == "timeout":
            phase = incident.get("sqlite_lock_phase") or "unknown"
            print(f"SQLite write lock: timeout ({phase})")
        elif incident.get("sqlite_write_lock") == "wait":
            print("SQLite write lock: wait")
        else:
            print("SQLite write lock: none")
        print(f"SQLite lock wait: {_format_ms(incident.get('wait_ms')) if incident.get('wait_ms') is not None else 'none'}")
        print(
            "Lock holder: "
            f"pid={incident.get('cleanup_holder_pid') or incident.get('lock_holder_pid') or '?'} "
            f"host={incident.get('cleanup_holder_host') or incident.get('lock_holder_host') or '?'} "
            f"source={incident.get('cleanup_holder_source') or incident.get('lock_holder_source') or '?'}"
        )
        if incident.get("lock_path"):
            print(f"Lock file: {incident.get('lock_path')}")
        if incident.get("cleanup"):
            cleanup = incident.get("cleanup")
            reason = incident.get("cleanup_reason") or "unknown"
            pid_status = incident.get("cleanup_pid_status") or "unknown"
            print(f"PID status: {pid_status}")
            if cleanup == "removed":
                print("Cleanup: removed local dead-PID lock")
                print("Result: write/startup continued")
            elif cleanup == "skipped":
                print(f"Cleanup: skipped, reason={reason}")
            elif cleanup == "failed":
                print(f"Cleanup: failed, reason={reason}")
        print(f"UI pause: {_format_ms(incident.get('ui_pause_ms')) if incident.get('ui_pause_ms') is not None else 'none'}")
        print(f"UI result: {incident.get('ui_result') or 'unknown'}")
        print(f"Maintenance overlap: {'yes' if incident.get('maintenance_overlap') else 'none'}")
        _print_shadow_mirror_text(incident)
        print(f"Maintenance resumed: {', '.join(resumed) if resumed else 'none'}")
        print(f"Conclusion: {_conclusion_text(incident.get('classification'))}")
        print()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    events = _load_events(Path(args.logs), args.date)
    returns = [event for event in events if event.kind == "user_return_from_idle" and event.ts is not None]
    incidents = [_incident_payload(event, _nearby(events, event.ts, args.window_sec)) for event in returns if event.ts]
    if args.json:
        print(json.dumps({"incidents": incidents, "count": len(incidents)}, ensure_ascii=False, indent=2))
    else:
        _print_text(incidents)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
