from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Tuple

EPOCH_SYNC_TS = "1970-01-01 00:00:00.000"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _normalize_timestamp_text(value: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%d %H:%M:%S.%f")[:23]


def normalize_sync_timestamp(value: Any, *, default_ts: str = EPOCH_SYNC_TS) -> str:
    default_text = _normalize_timestamp_text(str(default_ts)) or str(default_ts)
    return _normalize_timestamp_text(str(value or default_text)) or default_text


def normalize_sync_cursor(cursor: Any, *, default_ts: str = EPOCH_SYNC_TS) -> Tuple[str, int]:
    if cursor is None:
        return normalize_sync_timestamp(default_ts), 0

    if isinstance(cursor, Mapping):
        ts = normalize_sync_timestamp(
            cursor.get("updated_at")
            or cursor.get("last_sync_time")
            or cursor.get("ts")
            or default_ts,
            default_ts=default_ts,
        )
        row_id = _safe_int(cursor.get("id", 0))
        return ts, row_id

    if isinstance(cursor, (list, tuple)) and len(cursor) >= 2:
        ts = normalize_sync_timestamp(cursor[0] or default_ts, default_ts=default_ts)
        row_id = _safe_int(cursor[1], 0)
        return ts, row_id

    return normalize_sync_timestamp(cursor or default_ts, default_ts=default_ts), 0


def make_sync_cursor(updated_at: str, row_id: int) -> Dict[str, Any]:
    return {
        "updated_at": normalize_sync_timestamp(updated_at),
        "id": _safe_int(row_id, 0),
    }


def is_cursor_newer(
    candidate_ts: str,
    candidate_id: int,
    current_ts: str,
    current_id: int,
) -> bool:
    candidate_ts = normalize_sync_timestamp(candidate_ts)
    current_ts = normalize_sync_timestamp(current_ts)
    candidate_id = _safe_int(candidate_id, 0)
    current_id = _safe_int(current_id, 0)
    if candidate_ts > current_ts:
        return True
    if candidate_ts == current_ts and candidate_id > current_id:
        return True
    return False


def max_sync_cursor(*cursors: Any, default_ts: str = EPOCH_SYNC_TS) -> Dict[str, Any]:
    best_ts, best_id = normalize_sync_cursor(None, default_ts=default_ts)
    for cursor in cursors:
        ts, row_id = normalize_sync_cursor(cursor, default_ts=default_ts)
        if is_cursor_newer(ts, row_id, best_ts, best_id):
            best_ts, best_id = ts, row_id
    return make_sync_cursor(best_ts, best_id)
