import os
import threading
import time
from typing import Optional

from rem_card.app.logger import logger


_LOCK = threading.Lock()
_COUNTS = {
    "applied": 0,
    "stale_blocked": 0,
    "forced_reload": 0,
    "conflicts": 0,
}
_EVENTS_SINCE_SUMMARY = 0
_LAST_SUMMARY_TS = 0.0


def _summary_interval_sec() -> float:
    try:
        return max(30.0, float(os.getenv("REMCARD_ORDERS_SYNC_SUMMARY_SEC", "300")))
    except Exception:
        return 300.0


def record_orders_sync_event(
    event_name: str,
    *,
    role: str = "",
    admission_id: Optional[int] = None,
    context_hash: Optional[str] = None,
    reason: str = "",
    immediate: bool = False,
) -> None:
    global _EVENTS_SINCE_SUMMARY, _LAST_SUMMARY_TS

    event_key = str(event_name or "").strip().lower()
    if event_key == "conflict":
        counter_key = "conflicts"
    else:
        counter_key = event_key
    if counter_key not in _COUNTS:
        return

    now = time.monotonic()
    with _LOCK:
        _COUNTS[counter_key] += 1
        _EVENTS_SINCE_SUMMARY += 1
        should_log = (
            immediate
            or _LAST_SUMMARY_TS <= 0.0
            or _EVENTS_SINCE_SUMMARY >= 25
            or (now - _LAST_SUMMARY_TS) >= _summary_interval_sec()
        )
        if not should_log:
            return
        _LAST_SUMMARY_TS = now
        events_since_summary = _EVENTS_SINCE_SUMMARY
        _EVENTS_SINCE_SUMMARY = 0
        snapshot = dict(_COUNTS)

    logger.info(
        "[OrdersSync] ORDERS_SYNC_SUMMARY applied=%s stale_blocked=%s forced_reload=%s conflicts=%s "
        "events_since_summary=%s last_event=%s role=%s admission_id=%s context_hash=%s reason=%s",
        snapshot["applied"],
        snapshot["stale_blocked"],
        snapshot["forced_reload"],
        snapshot["conflicts"],
        events_since_summary,
        event_key,
        role,
        admission_id,
        context_hash,
        reason,
    )
