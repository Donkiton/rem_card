import argparse
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


TS_RE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[,.]\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?)"
)
KEY_VALUE_RE = re.compile(r"(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)=(?P<value>[^,\s]+)")
UI_PAUSE_RE = re.compile(r"event_loop_pause_ms=(?P<value>\d+(?:\.\d+)?)")
DATED_NAME_RE = re.compile(r"(\d{8}|\d{4}-\d{2}-\d{2})")

TEXT_NUMERIC_METRICS = {
    "backup_duration_ms",
    "integrity_check_duration_ms",
    "quick_check_duration_ms",
    "settings_read_ms",
    "orders_load_time_ms",
    "orders_snapshot_sql_step_ms",
    "orders_snapshot_build_total_ms",
    "build_orders_snapshot_time_ms",
    "central_io_lock_wait_ms",
    "db_lock_wait_ms",
    "write_queue_wait_ms",
    "read_duration_ms",
    "patient_vitals_elapsed_ms",
    "patient_card_elapsed_ms",
    "get_orders_elapsed_ms",
}

USER_VISIBLE_READ_METRICS = {
    "patient_vitals_elapsed_ms",
    "patient_card_elapsed_ms",
    "orders_load_time_ms",
    "get_orders_elapsed_ms",
    "settings_read_ms",
    "read_duration_ms",
}
BACKUP_METRICS = {"backup_duration_ms", "backup_created", "periodic_backup_created"}
INTEGRITY_METRICS = {"integrity_check_duration_ms", "integrity_check_passed"}
QUICK_CHECK_METRICS = {"quick_check_duration_ms"}
LOCK_METRICS = {"central_io_lock_wait_ms", "db_lock_wait_ms", "write_queue_wait_ms"}
CLASSIFICATIONS = (
    "maintenance_contention_integrity_check",
    "maintenance_contention_backup",
    "maintenance_contention_quick_check",
    "settings_snapshot_schema_drift",
    "emergency_deferred_metric_spam",
    "user_visible_read_stall",
    "UI_thread_sync_work",
    "write_lock_contention",
    "network_unavailable",
    "unknown",
)


@dataclass
class Event:
    ts: datetime | None
    kind: str
    value: float | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    source: str = ""


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze RemCard UI stalls and maintenance correlation.")
    parser.add_argument("--logs", required=True, help="Log directory or a single log/metrics file.")
    parser.add_argument("--date", help="Filter by date: YYYY-MM-DD or YYYYMMDD.")
    parser.add_argument("--role", help="Optional role filter, for example doctor or nurse.")
    parser.add_argument("--window-sec", type=float, default=60.0, help="Correlation window around UI pause.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON summary.")
    return parser.parse_args(argv)


def _parse_ts(raw: Any) -> datetime | None:
    if raw in (None, ""):
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    if not text:
        return None
    if "," in text:
        text = text.replace(",", ".")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        match = TS_RE.search(text)
        if not match:
            return None
        candidate = match.group("ts").replace(" ", "T").replace(",", ".").replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            return None


def _date_tokens(date_text: str | None) -> tuple[str, str] | None:
    if not date_text:
        return None
    compact = str(date_text).strip().replace("-", "")
    if len(compact) != 8 or not compact.isdigit():
        raise SystemExit("--date must be YYYY-MM-DD or YYYYMMDD")
    return compact, f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"


def _iter_files(root: Path, tokens: tuple[str, str] | None) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    suffixes = {".log", ".txt", ".jsonl"}
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        if tokens is not None:
            dated_tokens = {item.replace("-", "") for item in DATED_NAME_RE.findall(path.name)}
            if dated_tokens and tokens[0] not in dated_tokens:
                continue
        yield path


def _matches_date(ts: datetime | None, tokens: tuple[str, str] | None) -> bool:
    if tokens is None or ts is None:
        return True
    return ts.strftime("%Y%m%d") == tokens[0]


def _normalize_role(role: str | None) -> str:
    return str(role or "").strip().lower()


def _infer_role_from_path(source: str) -> str:
    text = str(source or "").replace("\\", "/").lower()
    if "nurse_log" in text or "/nurse" in text or "nurse_" in text or "медсестр" in text:
        return "nurse"
    if "/logs" in text and ("doctor" in text or "врач" in text):
        return "doctor"
    if "doctor" in text or "врач" in text:
        return "doctor"
    if "operblock" in text:
        return "operblock"
    return ""


def _ensure_role(fields: dict[str, Any], source: str) -> dict[str, Any]:
    result = dict(fields)
    if not str(result.get("role") or "").strip():
        inferred = _infer_role_from_path(source)
        if inferred:
            result["role"] = inferred
    return result


def _role_matches(fields: dict[str, Any], role: str | None) -> bool:
    expected = _normalize_role(role)
    if not expected:
        return True
    return _normalize_role(str(fields.get("role") or "")) == expected


def _event_from_metric(payload: dict[str, Any], source: str) -> Event | None:
    metric = str(payload.get("metric") or "")
    if not metric:
        return None
    value = payload.get("value")
    try:
        numeric_value = float(value) if value not in (None, "") else None
    except Exception:
        numeric_value = None
    fields = {key: val for key, val in payload.items() if key not in {"metric", "value", "ts"}}
    fields = _ensure_role(fields, source)
    return Event(ts=_parse_ts(payload.get("ts")), kind=metric, value=numeric_value, fields=fields, source=source)


def _events_from_text_line(line: str, source: str) -> list[Event]:
    ts = _parse_ts(line)
    fields = {match.group("key"): match.group("value") for match in KEY_VALUE_RE.finditer(line)}
    fields = _ensure_role(fields, source)
    fields["raw"] = line
    lower = line.lower()
    events: list[Event] = []
    pause_match = UI_PAUSE_RE.search(line)
    if pause_match:
        events.append(Event(ts=ts, kind="event_loop_pause_ms", value=float(pause_match.group("value")), fields=fields, source=source))

    for metric in TEXT_NUMERIC_METRICS:
        if metric in fields:
            events.append(Event(ts=ts, kind=metric, value=_safe_float(fields.get(metric)), fields=fields, source=source))
        elif metric in line:
            events.append(Event(ts=ts, kind=metric, value=_safe_float(fields.get("value")), fields=fields, source=source))

    elapsed_ms = _safe_float(fields.get("elapsed_ms"))
    if elapsed_ms is not None:
        if "patient_vitals" in lower or "vitals" in lower:
            events.append(Event(ts=ts, kind="patient_vitals_elapsed_ms", value=elapsed_ms, fields=fields, source=source))
        elif "patient_card" in lower or "load_patient_card" in lower:
            events.append(Event(ts=ts, kind="patient_card_elapsed_ms", value=elapsed_ms, fields=fields, source=source))
        elif "get_orders" in lower:
            events.append(Event(ts=ts, kind="get_orders_elapsed_ms", value=elapsed_ms, fields=fields, source=source))

    if "periodic backup created" in lower or "periodic_backup" in lower and "created" in lower:
        events.append(Event(ts=ts, kind="periodic_backup_created", fields=fields, source=source))
    if "background integrity_check passed" in lower:
        events.append(Event(ts=ts, kind="integrity_check_passed", fields=fields, source=source))
    if "emergency_startup_failed" in lower or "settings snapshot validation failed" in lower:
        events.append(Event(ts=ts, kind="emergency_startup_failed", fields=fields, source=source))
    if "missing settings tables" in lower or "operblock_icons" in lower:
        events.append(Event(ts=ts, kind="emergency_settings_snapshot_schema_drift", fields=fields, source=source))
    if "[orderssync] nurse_optimistic_conflict" in lower:
        events.append(Event(ts=ts, kind="orders_optimistic_conflict", fields=fields, source=source))
    if "windows fatal exception" in lower or "fatal exception" in lower or "traceback (most recent call last)" in lower:
        events.append(Event(ts=ts, kind="fault_crash", fields=fields, source=source))
    return events


def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _ts_sort_value(ts: datetime | None) -> float:
    if ts is None:
        return float("-inf")
    if ts.tzinfo is not None and ts.utcoffset() is not None:
        return ts.astimezone(timezone.utc).timestamp()
    return ts.timestamp()


def _load_events(root: Path, tokens: tuple[str, str] | None, role: str | None) -> list[Event]:
    events: list[Event] = []
    for path in _iter_files(root, tokens):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if stripped.startswith("{"):
                        try:
                            event = _event_from_metric(json.loads(stripped), str(path))
                        except Exception:
                            event = None
                        if event and _matches_date(event.ts, tokens) and _role_matches(event.fields, role):
                            events.append(event)
                        continue
                    for event in _events_from_text_line(stripped, str(path)):
                        if _matches_date(event.ts, tokens) and _role_matches(event.fields, role):
                            events.append(event)
        except OSError:
            continue
    events.sort(key=lambda event: _ts_sort_value(event.ts))
    return events


def _nearby(events: list[Event], center: datetime, *, window_sec: float) -> list[Event]:
    center_value = _ts_sort_value(center)
    return [
        event
        for event in events
        if event.ts is not None and abs(_ts_sort_value(event.ts) - center_value) <= float(window_sec)
    ]


def _raw_text(event: Event) -> str:
    return str(event.fields.get("raw") or event.fields.get("reason") or event.fields.get("detail") or "")


def _related_metric(related: list[Event], kinds: set[str], threshold_ms: float = 0.0) -> Event | None:
    matches = [event for event in related if event.kind in kinds and (event.value is None or (event.value or 0.0) >= threshold_ms)]
    if not matches:
        return None
    return max(matches, key=lambda event: event.value or 0.0)


def _related_task(related: list[Event], names: set[str]) -> Event | None:
    for event in related:
        if event.kind not in {"maintenance_task_started", "maintenance_task_finished", "maintenance_task_failed"}:
            continue
        task_type = str(event.fields.get("task_type") or event.fields.get("task") or "")
        if task_type in names:
            return event
    return None


def _has_text_marker(events: list[Event], markers: tuple[str, ...]) -> bool:
    for event in events:
        text = " ".join(str(value) for value in event.fields.values()).lower()
        text = f"{event.kind} {text}"
        if any(marker in text for marker in markers):
            return True
    return False


def _classify_pause(pause: Event, related: list[Event]) -> tuple[str, str]:
    if _has_text_marker(related, ("operblock_icons", "missing settings tables", "invalid_snapshot_schema_drift")):
        return "settings_snapshot_schema_drift", "settings snapshot schema drift near pause"

    if _has_text_marker(related, ("network is unreachable", "unable to open database file", "source_unavailable")):
        return "network_unavailable", "network/source unavailable near pause"

    integrity = _related_metric(related, INTEGRITY_METRICS, threshold_ms=1000.0) or _related_task(related, {"integrity_check"})
    if integrity is not None:
        return "maintenance_contention_integrity_check", _format_related("integrity_check", integrity)

    backup = (
        _related_metric(related, BACKUP_METRICS, threshold_ms=1000.0)
        or _related_task(related, {"periodic_backup", "daily_backup", "backup_validation"})
    )
    if backup is not None:
        return "maintenance_contention_backup", _format_related("backup", backup)

    quick = _related_metric(related, QUICK_CHECK_METRICS, threshold_ms=1000.0) or _related_task(related, {"quick_check"})
    if quick is not None:
        return "maintenance_contention_quick_check", _format_related("quick_check", quick)

    lock_wait = _related_metric(related, LOCK_METRICS, threshold_ms=500.0)
    if lock_wait is not None or _has_text_marker(related, ("database is locked", "database is busy", "write_queue_not_idle")):
        return "write_lock_contention", _format_related("lock_wait", lock_wait) if lock_wait else "lock/busy marker near pause"

    slow_read = _related_metric(related, USER_VISIBLE_READ_METRICS, threshold_ms=1000.0)
    if slow_read is not None:
        return "user_visible_read_stall", _format_related("user_visible_read", slow_read)

    if (pause.value or 0.0) >= 750.0:
        return "UI_thread_sync_work", "UI event loop pause without matching maintenance/read metric"
    return "unknown", "no matching stall marker"


def _format_related(label: str, event: Event | None) -> str:
    if event is None:
        return label
    if event.value is None:
        return f"{label}:{event.kind}"
    return f"{label}:{event.kind}:{round(event.value, 1)}ms"


def _classify_event(event: Event) -> str:
    text = f"{event.kind} {' '.join(str(value) for value in event.fields.values())}".lower()
    if "operblock_icons" in text or "missing settings tables" in text or "invalid_snapshot_schema_drift" in text:
        return "settings_snapshot_schema_drift"
    if "network is unreachable" in text or "unable to open database file" in text or "source_unavailable" in text:
        return "network_unavailable"
    return "unknown"


def _deferred_attempt_count(events: list[Event]) -> int:
    raw_count = sum(1 for event in events if event.kind == "emergency_standby_refresh_deferred")
    summaries = [
        int(event.value if event.value is not None else event.fields.get("count") or 0)
        for event in events
        if event.kind == "emergency_standby_refresh_deferred_summary"
    ]
    return max(raw_count, sum(summaries)) if summaries else raw_count


def _rate_per_hour(events: list[Event], count: int) -> float:
    timed = [event for event in events if event.ts is not None]
    if len(timed) < 2:
        return float(count) if count else 0.0
    seconds = max(1.0, _ts_sort_value(timed[-1].ts) - _ts_sort_value(timed[0].ts))
    return float(count) * 3600.0 / seconds


def _top_slow_metrics(events: list[Event], limit: int = 5) -> list[dict[str, Any]]:
    candidates = [event for event in events if event.kind in USER_VISIBLE_READ_METRICS | BACKUP_METRICS | INTEGRITY_METRICS | QUICK_CHECK_METRICS | LOCK_METRICS and event.value is not None]
    candidates.sort(key=lambda event: event.value or 0.0, reverse=True)
    return [
        {
            "ts": None if event.ts is None else event.ts.isoformat(),
            "metric": event.kind,
            "value": event.value,
            "role": event.fields.get("role"),
            "source": event.source,
        }
        for event in candidates[:limit]
    ]


def build_summary(events: list[Event], *, window_sec: float) -> dict[str, Any]:
    pauses = [event for event in events if event.kind == "event_loop_pause_ms"]
    conflicts = [event for event in events if event.kind in {"orders_optimistic_conflict", "order_action_pending_blocked"}]
    deferred = [event for event in events if event.kind == "maintenance_task_deferred"]
    emergency_deferred = [event for event in events if event.kind == "emergency_standby_refresh_deferred"]
    emergency_deferred_summaries = [event for event in events if event.kind == "emergency_standby_refresh_deferred_summary"]
    emergency_attempts = _deferred_attempt_count(events)
    emergency_rate = _rate_per_hour(emergency_deferred or emergency_deferred_summaries, emergency_attempts)
    classifications = {name: 0 for name in CLASSIFICATIONS}
    explained_pauses = []
    for pause in pauses:
        if pause.ts is None:
            continue
        related = _nearby(events, pause.ts, window_sec=window_sec)
        classification, cause = _classify_pause(pause, related)
        classifications[classification] = classifications.get(classification, 0) + 1
        explained_pauses.append(
            {
                "ts": pause.ts.isoformat(),
                "pause_ms": pause.value,
                "role": pause.fields.get("role"),
                "tab_name": pause.fields.get("tab_name") or pause.fields.get("tab"),
                "admission_id": pause.fields.get("admission_id"),
                "classification": classification,
                "cause": cause,
                "related_metrics": sorted({event.kind for event in related if event.kind != "event_loop_pause_ms"}),
            }
        )

    schema_drift_events = [
        event
        for event in events
        if event.kind in {"emergency_startup_failed", "emergency_settings_snapshot_schema_drift"}
        and _classify_event(event) == "settings_snapshot_schema_drift"
    ]
    if schema_drift_events:
        classifications["settings_snapshot_schema_drift"] = classifications.get("settings_snapshot_schema_drift", 0) + len(schema_drift_events)
    if emergency_attempts >= 100 or emergency_rate >= 60.0:
        classifications["emergency_deferred_metric_spam"] = classifications.get("emergency_deferred_metric_spam", 0) + 1

    return {
        "event_count": len(events),
        "ui_pause_count": len(pauses),
        "ui_pauses": explained_pauses,
        "classifications": {key: value for key, value in classifications.items() if value},
        "maintenance_deferred_count": len(deferred),
        "order_conflict_count": len(conflicts),
        "emergency_startup_failed_count": sum(1 for event in events if event.kind == "emergency_startup_failed"),
        "settings_snapshot_schema_drift_count": len(schema_drift_events),
        "emergency_standby_refresh_deferred_count": len(emergency_deferred),
        "emergency_standby_refresh_deferred_summary_count": len(emergency_deferred_summaries),
        "emergency_standby_refresh_deferred_attempt_count_estimate": emergency_attempts,
        "emergency_standby_refresh_deferred_rate_per_hour": round(emergency_rate, 3),
        "fault_crash_count": sum(1 for event in events if event.kind == "fault_crash"),
        "top_slow_metrics": _top_slow_metrics(events),
        "top_metric_counts": _metric_counts(events),
    }


def _metric_counts(events: list[Event]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        counts[event.kind] = counts.get(event.kind, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:30])


def _print_human(summary: dict[str, Any]) -> None:
    print(f"Events: {summary['event_count']}")
    print(f"UI pauses: {summary['ui_pause_count']}")
    print("Top UIWatchdog pauses:")
    for pause in sorted(summary["ui_pauses"], key=lambda item: item.get("pause_ms") or 0.0, reverse=True)[:20]:
        print(
            f"- {pause['ts']} pause_ms={pause['pause_ms']} role={pause.get('role')} "
            f"tab={pause.get('tab_name')} admission_id={pause.get('admission_id')} "
            f"classification={pause['classification']} cause={pause['cause']}"
        )
    print("Classifications:")
    for key, value in sorted(summary["classifications"].items(), key=lambda item: (-item[1], item[0])):
        print(f"- {key}: {value}")
    print("Top slow metrics:")
    for item in summary["top_slow_metrics"]:
        print(f"- {item['ts']} {item['metric']}={item['value']} role={item.get('role')}")
    print(f"Maintenance deferrals: {summary['maintenance_deferred_count']}")
    print(f"Order pending/conflict events: {summary['order_conflict_count']}")
    print(f"Emergency startup failed: {summary['emergency_startup_failed_count']}")
    print(f"Settings snapshot schema drift: {summary['settings_snapshot_schema_drift_count']}")
    print(
        "Emergency standby deferred: "
        f"events={summary['emergency_standby_refresh_deferred_count']} "
        f"summaries={summary['emergency_standby_refresh_deferred_summary_count']} "
        f"attempt_estimate={summary['emergency_standby_refresh_deferred_attempt_count_estimate']} "
        f"rate_per_hour={summary['emergency_standby_refresh_deferred_rate_per_hour']}"
    )
    print(f"Fault/crash markers: {summary['fault_crash_count']}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    root = Path(args.logs)
    events = _load_events(root, _date_tokens(args.date), args.role)
    summary = build_summary(events, window_sec=max(1.0, float(args.window_sec or 1.0)))
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    else:
        _print_human(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
