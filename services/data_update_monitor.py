import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from PySide6.QtCore import QThread, Signal

from rem_card.app.db_availability import DatabaseClosedError
from rem_card.app.logger import logger
from rem_card.app.local_metrics import record_metric


class DataUpdateMonitor(QThread):
    changes_detected = Signal(dict)
    monitor_error = Signal(str)

    def __init__(self, data_service, *, poll_interval_sec: float = 2.0):
        super().__init__()
        self._data_service = data_service
        self._poll_interval_sec = max(0.5, float(poll_interval_sec))
        self._stop_evt = threading.Event()
        self._wake_evt = threading.Event()
        self._state_lock = threading.Lock()
        self._force_emit = False
        self._force_sources: list[str] = []
        self._last_seen_id: Optional[int] = None
        self._last_seen_observed_monotonic: float = 0.0
        self._refresh_request_seq: int = 0
        self._last_observed_refresh_seq: int = 0
        self._state_epoch: int = 0

    def request_refresh(self, *, force_emit: bool = False, source: str = ""):
        with self._state_lock:
            self._refresh_request_seq += 1
            if force_emit:
                self._force_emit = True
                if source:
                    self._force_sources.append(str(source))
        self._wake_evt.set()

    def reset(self):
        with self._state_lock:
            self._last_seen_id = None
            self._last_seen_observed_monotonic = 0.0
            self._force_emit = False
            self._force_sources = []
            self._refresh_request_seq += 1
            self._state_epoch += 1
        self._wake_evt.set()

    def get_change_state(self) -> Optional[dict[str, Any]]:
        with self._state_lock:
            if self._last_seen_id is None:
                return None
            return {
                "change_id": int(self._last_seen_id),
                "observed_monotonic": float(self._last_seen_observed_monotonic),
                "refresh_request_seq": int(self._refresh_request_seq),
                "refresh_observed_seq": int(self._last_observed_refresh_seq),
                "state_epoch": int(self._state_epoch),
            }

    def _set_last_seen_id(self, change_id: int, *, observed_refresh_seq: int) -> None:
        with self._state_lock:
            self._last_seen_id = int(change_id)
            self._last_seen_observed_monotonic = time.monotonic()
            self._last_observed_refresh_seq = max(
                self._last_observed_refresh_seq,
                int(observed_refresh_seq),
            )

    def _increment_state_epoch(self) -> None:
        with self._state_lock:
            self._state_epoch += 1

    def stop(self):
        self._stop_evt.set()
        self._wake_evt.set()

    def _is_shutting_down(self) -> bool:
        return self._stop_evt.is_set() or bool(getattr(self._data_service, "_shutting_down", False))

    def _should_suppress_poll_error(self, exc: Exception) -> bool:
        if not self._is_shutting_down():
            return False
        if isinstance(exc, DatabaseClosedError):
            return True
        return "database connection is closed" in str(exc).lower()

    def run(self):
        while not self._stop_evt.is_set():
            force_emit = False
            force_sources: list[str] = []
            with self._state_lock:
                force_emit = self._force_emit
                force_sources = list(self._force_sources)
                self._force_emit = False
                self._force_sources = []

            try:
                self._poll_once(force_emit=force_emit, force_sources=force_sources)
            except Exception as exc:
                if self._should_suppress_poll_error(exc):
                    logger.info("DataUpdateMonitor stopped during shutdown after database connection closed")
                    return
                logger.error("DataUpdateMonitor poll failed: %s", exc, exc_info=True)
                self.monitor_error.emit(str(exc))

            if self._stop_evt.is_set():
                return
            if self._wake_evt.wait(self._poll_interval_sec):
                self._wake_evt.clear()

    def _poll_once(self, *, force_emit: bool, force_sources: list[str]):
        run_maintenance = getattr(self._data_service, "run_poll_maintenance_tasks", None)
        if callable(run_maintenance):
            run_maintenance()

        if self._stop_evt.is_set():
            return

        with self._state_lock:
            previous_change_id = self._last_seen_id
            observed_refresh_seq = int(self._refresh_request_seq)

        current_change_id = int(self._data_service.get_latest_change_id())

        if self._stop_evt.is_set():
            return

        if previous_change_id is None:
            self._set_last_seen_id(current_change_id, observed_refresh_seq=observed_refresh_seq)
            if force_emit:
                self._emit_payload(
                    current_change_id=current_change_id,
                    previous_change_id=current_change_id,
                    changes=[],
                    forced=True,
                    force_sources=force_sources,
                )
            return

        if current_change_id < previous_change_id:
            logger.warning(
                "Change-log cursor moved backwards: previous=%s current=%s. Forcing full refresh.",
                previous_change_id,
                current_change_id,
            )
            self._increment_state_epoch()
            self._set_last_seen_id(current_change_id, observed_refresh_seq=observed_refresh_seq)
            self._emit_payload(
                current_change_id=current_change_id,
                previous_change_id=previous_change_id,
                changes=[],
                forced=True,
                gap_detected=True,
                reason="cursor_moved_backwards",
                force_sources=force_sources,
            )
            return

        if current_change_id > previous_change_id:
            rows = self._data_service.fetch_changes_since(previous_change_id)
            changes = [self._normalize_row(row) for row in rows]
            self._set_last_seen_id(current_change_id, observed_refresh_seq=observed_refresh_seq)
            if not changes:
                logger.warning(
                    "Change-log gap suspected: previous=%s current=%s rows=0. Forcing full refresh.",
                    previous_change_id,
                    current_change_id,
                )
                self._emit_payload(
                    current_change_id=current_change_id,
                    previous_change_id=previous_change_id,
                    changes=[],
                    forced=True,
                    gap_detected=True,
                    reason="empty_change_rows",
                    force_sources=force_sources,
                )
                return
            self._emit_payload(
                current_change_id=current_change_id,
                previous_change_id=previous_change_id,
                changes=changes,
                forced=force_emit,
                force_sources=force_sources,
            )
            return

        self._set_last_seen_id(current_change_id, observed_refresh_seq=observed_refresh_seq)
        if force_emit:
            self._emit_payload(
                current_change_id=current_change_id,
                previous_change_id=previous_change_id,
                changes=[],
                forced=True,
                force_sources=force_sources,
            )

    def _emit_payload(
        self,
        *,
        current_change_id: int,
        previous_change_id: int,
        changes: list[dict[str, Any]],
        forced: bool,
        force_sources: list[str] | None = None,
        gap_detected: bool = False,
        reason: str = "",
    ):
        changed_entities = sorted(
            {
                str(change.get("entity_name"))
                for change in changes
                if change.get("entity_name")
            }
        )
        admission_ids = sorted(
            {
                int(change["admission_id"])
                for change in changes
                if change.get("admission_id") is not None
            }
        )
        payload = {
            "scope": "global",
            "previous_change_id": int(previous_change_id),
            "last_change_id": int(current_change_id),
            "forced": bool(forced),
            "gap_detected": bool(gap_detected),
            "reason": str(reason or ""),
            "force_source": str((force_sources or [""])[-1] or ""),
            "force_sources": list(force_sources or []),
            "changes": changes,
            "changed_entities": changed_entities,
            "admission_ids": admission_ids,
        }
        record_metric("latest_change_id", int(current_change_id), component="DataUpdateMonitor")
        if forced:
            record_metric("forced_refresh_count", 1, reason=str(reason or ""), force_sources=list(force_sources or []))
        if gap_detected or str(reason or "") in {"cursor_moved_backwards", "empty_change_rows"}:
            record_metric("full_snapshot_count", 1, reason=str(reason or "gap_detected"))
        lag_ms = self._change_log_lag_ms(changes)
        if lag_ms is not None:
            record_metric("change_log_lag_ms", lag_ms, last_change_id=int(current_change_id))
        self.changes_detected.emit(payload)

    @staticmethod
    def _change_log_lag_ms(changes: list[dict[str, Any]]) -> Optional[int]:
        latest_ts = ""
        for change in changes or []:
            changed_at = str(change.get("changed_at") or "")
            if changed_at > latest_ts:
                latest_ts = changed_at
        if not latest_ts:
            return None
        try:
            normalized = latest_ts.replace("Z", "+00:00")
            if "T" not in normalized and " " in normalized:
                normalized = normalized.replace(" ", "T", 1)
            changed_dt = datetime.fromisoformat(normalized)
            if changed_dt.tzinfo is not None:
                lag_sec = time.time() - changed_dt.timestamp()
            else:
                # SQLite CURRENT_TIMESTAMP is UTC but stored without a timezone suffix.
                lag_sec = time.time() - changed_dt.replace(tzinfo=timezone.utc).timestamp()
            return max(0, int(lag_sec * 1000.0))
        except Exception:
            return None

    @staticmethod
    def _normalize_row(row: Any) -> dict[str, Any]:
        if row is None:
            return {}
        if hasattr(row, "keys"):
            return {
                "id": int(row["id"]) if row["id"] is not None else None,
                "entity_name": row["entity_name"],
                "entity_id": row["entity_id"],
                "admission_id": row["admission_id"],
                "action": row["action"],
                "changed_at": row["changed_at"],
                "changed_by": row["changed_by"],
                "version": row["version"],
            }
        return {
            "id": int(row[0]) if len(row) > 0 and row[0] is not None else None,
            "entity_name": row[1] if len(row) > 1 else None,
            "entity_id": row[2] if len(row) > 2 else None,
            "admission_id": row[3] if len(row) > 3 else None,
            "action": row[4] if len(row) > 4 else None,
            "changed_at": row[5] if len(row) > 5 else None,
            "changed_by": row[6] if len(row) > 6 else None,
            "version": row[7] if len(row) > 7 else None,
        }
