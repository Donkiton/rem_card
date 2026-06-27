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
    "opblock_shadow_mirror_started",
    "opblock_shadow_mirror_finished",
    "opblock_shadow_mirror_failed",
    "maintenance_overlap_observed",
    "ui_pending_state_observed",
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
    if _first(related, "sqlite_write_lock_timeout"):
        return "sqlite_write_lock_timeout"
    if _first(related, "sqlite_write_lock_wait_retry", "sqlite_write_lock_wait_started"):
        return "sqlite_write_lock_wait"
    if _first(related, "maintenance_overlap_observed"):
        return "maintenance_overlap"
    if _first(related, "opblock_shadow_mirror_failed"):
        return "shadow_mirror_failure"
    pending = [event for event in related if event.kind == "ui_pending_state_observed"]
    finished = _first(related, "opblock_action_finished")
    if pending and not finished:
        return "ui_pending_stuck"
    if _first(related, "event_loop_pause_ms"):
        return "unknown_ui_pause"
    return "unknown"


def _incident_payload(event: Event, related: list[Event]) -> dict[str, Any]:
    action = _first(related, "opblock_action_started") or event
    finished = _first(related, "opblock_action_finished")
    lock_event = _first(related, "sqlite_write_lock_timeout", "sqlite_write_lock_wait_retry", "sqlite_write_lock_acquired")
    ui_pause = _first(related, "event_loop_pause_ms")
    return {
        "incident_at": (event.ts.isoformat(sep=" ") if event.ts else ""),
        "idle_ms": event.fields.get("idle_ms"),
        "first_action": event.fields.get("first_action") or action.fields.get("action"),
        "result": (finished.fields.get("result") if finished else _classify(related)),
        "classification": _classify(related),
        "wait_ms": (lock_event.fields.get("total_wait_ms") if lock_event else None),
        "lock_holder_pid": (lock_event.fields.get("lock_holder_pid") if lock_event else None),
        "lock_holder_host": (lock_event.fields.get("lock_holder_host") if lock_event else ""),
        "lock_holder_source": (lock_event.fields.get("lock_holder_source") if lock_event else ""),
        "ui_pause_ms": (ui_pause.value if ui_pause else None),
        "maintenance_overlap": bool(_first(related, "maintenance_overlap_observed")),
        "shadow_mirror_overlap": bool(_first(related, "opblock_shadow_mirror_started", "opblock_shadow_mirror_failed")),
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


def _print_text(incidents: list[dict[str, Any]]) -> None:
    if not incidents:
        print("No opblock idle/resume incidents found.")
        return
    for incident in incidents:
        print(f"Incident: {incident['incident_at']}")
        print(f"Idle before action: {_format_ms(incident.get('idle_ms'))}")
        print(f"First action: {incident.get('first_action') or 'unknown'}")
        print(f"Result: {incident.get('result') or 'unknown'}")
        print(f"Wait: {_format_ms(incident.get('wait_ms'))}")
        print(
            "Lock holder: "
            f"pid={incident.get('lock_holder_pid') or '?'} "
            f"host={incident.get('lock_holder_host') or '?'} "
            f"source={incident.get('lock_holder_source') or '?'}"
        )
        print(f"UI pause: {_format_ms(incident.get('ui_pause_ms'))}")
        print(f"Maintenance overlap: {'yes' if incident.get('maintenance_overlap') else 'none'}")
        print(f"Shadow mirror overlap: {'yes' if incident.get('shadow_mirror_overlap') else 'none'}")
        print(f"Conclusion: {incident.get('classification')}")
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
