from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable


HEALTH_SCHEMA_VERSION = 1
DEFAULT_STALE_AFTER_SEC = 24 * 60 * 60
UNCHANGED_PERSIST_INTERVAL_SEC = 60
_DELIVERY_LOCK = threading.Lock()
_DELIVERY_STATE: dict[str, dict[str, bool]] = {}


def build_local_replica_health_path(local_db_path: str) -> str:
    return f"{os.path.abspath(local_db_path)}.health.json"


def _database_key(database_path: str) -> str:
    normalized = os.path.normcase(
        os.path.abspath(os.path.normpath(str(database_path or "")))
    )
    return hashlib.sha256(
        normalized.encode("utf-8", errors="surrogatepass")
    ).hexdigest()[:16]


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
        except OSError:
            pass


def request_local_crash_delivery(data_root: str) -> bool:
    """Request one serialized daemon delivery attempt for an explicit root."""
    root = os.path.abspath(os.path.normpath(str(data_root or "")))
    if not root:
        return False
    key = os.path.normcase(root)
    with _DELIVERY_LOCK:
        state = _DELIVERY_STATE.setdefault(
            key,
            {"running": False, "pending": False},
        )
        if state["running"]:
            state["pending"] = True
            return False
        state["running"] = True
        state["pending"] = False

    def deliver() -> None:
        logger = logging.getLogger(__name__)
        while True:
            try:
                from rem_card.services.crash_reports import flush_local_crash_outbox

                result = flush_local_crash_outbox(data_root=root)
                if result.get("delivered") or result.get("failed"):
                    logger.info(
                        "Local replica diagnostic delivery result: %s",
                        result,
                    )
            except Exception as exc:
                logger.warning(
                    "Local replica diagnostic delivery failed for %s: %s",
                    root,
                    exc,
                )
            with _DELIVERY_LOCK:
                state = _DELIVERY_STATE.get(key)
                if state is not None and state["pending"]:
                    state["pending"] = False
                    continue
                if state is not None:
                    state["running"] = False
                return

    threading.Thread(
        target=deliver,
        name=f"RemCardReplicaReportDelivery:{hashlib.sha256(key.encode()).hexdigest()[:8]}",
        daemon=True,
    ).start()
    return True


class LocalReplicaRoleHealth:
    """Persistent role-entry health for one PC/role/database replica.

    New snapshot installation and successful unchanged verification are stored
    separately. File mtimes never count as proof of either outcome. The
    published snapshot is memory-only so GUI reads cannot touch the filesystem.
    """

    def __init__(
        self,
        *,
        state_path: str,
        role: str,
        database_path: str,
        client_id: str = "",
        stale_after_sec: float = DEFAULT_STALE_AFTER_SEC,
        reporter: Callable[[dict[str, Any]], object] | None = None,
        delivery_requester: Callable[[str], object] | None = request_local_crash_delivery,
        clock: Callable[[], float] = time.time,
        logger: logging.Logger | None = None,
    ) -> None:
        self.state_path = Path(state_path)
        self.role = str(role or "").strip().lower()
        self.client_id = str(client_id or "").strip()
        self.database_key = _database_key(database_path)
        self.data_root = os.path.dirname(os.path.dirname(os.path.abspath(database_path)))
        self.stale_after_sec = max(1.0, float(stale_after_sec))
        self._reporter = reporter or self._capture_pending_diagnostic
        self._delivery_requester = delivery_requester
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)
        self._lock = threading.Lock()
        self._initialized = False
        self._last_persisted_at = 0.0
        self._success_recorded_this_instance = False
        self._state = self._base_state({})
        self._published = self._publishable(self._state, now=self._safe_now())

    def record_success(self, outcome: str) -> None:
        normalized = str(outcome or "").strip().lower()
        if normalized not in {"snapshot_ready", "unchanged"}:
            return
        self._ensure_initialized()
        now = self._safe_now()
        with self._lock:
            state = dict(self._state)
            was_incident = bool(state.get("incident_active"))
            first_success_this_instance = not self._success_recorded_this_instance
            self._success_recorded_this_instance = True
            if normalized == "snapshot_ready":
                state["actual_copy_installed_at"] = now
            else:
                state["last_unchanged_verified_at"] = now
            state["last_successful_outcome"] = normalized
            state["last_successful_at"] = now
            state["incident_active"] = False
            state["incident_started_at"] = 0.0
            state["incident_reported_at"] = 0.0
            state["incident_outcome"] = ""
            state["incident_error_class"] = ""
            if was_incident:
                state["last_recovered_at"] = now
            should_persist = bool(
                normalized == "snapshot_ready"
                or was_incident
                or first_success_this_instance
                or (now - self._last_persisted_at) >= UNCHANGED_PERSIST_INTERVAL_SEC
            )
            payload = self._replace_state_locked(state, now=now)
        if should_persist:
            self._persist(payload)

    def record_role_entry_failure(
        self,
        *,
        outcome: str,
        local_copy_valid: bool,
        error_class: str = "",
        error: str = "",
    ) -> bool:
        """Create at most one pending diagnostic for the current stale incident."""
        self._ensure_initialized()
        now = self._safe_now()
        with self._lock:
            state = dict(self._state)
            confirmed_at = self._latest_confirmation_at(state, now=now)
            confirmed_age_sec = (
                max(0.0, now - confirmed_at) if confirmed_at > 0.0 else None
            )
            recent = bool(
                local_copy_valid
                and confirmed_at > 0.0
                and confirmed_age_sec is not None
                and confirmed_age_sec <= self.stale_after_sec
            )
            state["last_role_entry_attempt_at"] = now
            state["last_role_entry_outcome"] = str(outcome or "failed")
            state["last_role_entry_error_class"] = str(error_class or "")
            if not recent:
                if not state.get("incident_active"):
                    state["incident_active"] = True
                    state["incident_started_at"] = now
                    state["incident_reported_at"] = 0.0
                state["incident_outcome"] = str(outcome or "failed")
                state["incident_error_class"] = str(error_class or "")
            already_reported = self._number(
                state.get("incident_reported_at"), default=0.0
            ) > 0.0
            payload = self._replace_state_locked(state, now=now)
        self._persist(payload)
        if recent or already_reported:
            return False

        details = {
            "failure_kind": "stale_local_replica",
            "phase": "role_entry",
            "check_result": str(outcome or "failed"),
            "database_key": self.database_key,
            "client_id": self.client_id,
            "actual_copy_installed_at": self._number(
                state.get("actual_copy_installed_at"), default=0.0
            ),
            "last_unchanged_verified_at": self._number(
                state.get("last_unchanged_verified_at"), default=0.0
            ),
            "confirmed_age_sec": confirmed_age_sec,
            "never_confirmed": confirmed_at <= 0.0,
            "local_copy_valid": bool(local_copy_valid),
            "error_class": str(error_class or ""),
            "error": str(error or ""),
        }
        try:
            reported = bool(self._reporter(details))
        except Exception as exc:
            reported = False
            self._logger.warning(
                "Failed to create local replica role-entry diagnostic: %s",
                exc,
            )
        if not reported:
            return False

        with self._lock:
            state = dict(self._state)
            if state.get("incident_active"):
                state["incident_reported_at"] = now
            payload = self._replace_state_locked(state, now=now)
        self._persist(payload)
        self.request_pending_delivery()
        return True

    def request_pending_delivery(self) -> bool:
        if self._delivery_requester is None:
            return False
        try:
            return bool(self._delivery_requester(self.data_root))
        except Exception as exc:
            self._logger.warning(
                "Failed to schedule local replica diagnostic delivery: %s",
                exc,
            )
            return False

    def snapshot(self) -> dict[str, Any]:
        # No lazy initialization here: GUI health reads must never do file I/O.
        with self._lock:
            result = dict(self._published)
        confirmed_at = self._number(result.get("last_confirmed_at"), default=0.0)
        result["last_confirmed_age_sec"] = (
            max(0.0, self._safe_now() - confirmed_at)
            if confirmed_at > 0.0
            else None
        )
        return result

    def _ensure_initialized(self) -> None:
        with self._lock:
            if self._initialized:
                return
        payload = self._read_state_file()
        now = self._safe_now()
        with self._lock:
            if self._initialized:
                return
            self._state = self._base_state(payload)
            self._published = self._publishable(self._state, now=now)
            self._last_persisted_at = self._number(
                self._state.get("updated_at"),
                default=0.0,
            )
            self._initialized = True

    def _read_state_file(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        if (
            payload.get("schema_version") != HEALTH_SCHEMA_VERSION
            or str(payload.get("database_key") or "") != self.database_key
            or str(payload.get("role") or "") != self.role
        ):
            return {}
        return payload

    def _base_state(self, source: dict[str, Any]) -> dict[str, Any]:
        result = dict(source)
        result.update(
            {
                "schema_version": HEALTH_SCHEMA_VERSION,
                "database_key": self.database_key,
                "role": self.role,
                "client_id": self.client_id,
            }
        )
        for key in (
            "actual_copy_installed_at",
            "last_unchanged_verified_at",
            "incident_started_at",
            "incident_reported_at",
            "last_successful_at",
            "last_recovered_at",
            "last_role_entry_attempt_at",
            "updated_at",
        ):
            result[key] = self._number(result.get(key), default=0.0)
        result["incident_active"] = result.get("incident_active") is True
        return result

    def _replace_state_locked(
        self,
        state: dict[str, Any],
        *,
        now: float,
    ) -> dict[str, Any]:
        payload = self._base_state(state)
        payload["updated_at"] = now
        self._state = payload
        self._published = self._publishable(payload, now=now)
        self._initialized = True
        return dict(payload)

    def _publishable(self, state: dict[str, Any], *, now: float) -> dict[str, Any]:
        result = dict(state)
        confirmed_at = self._latest_confirmation_at(result, now=now)
        result["last_confirmed_at"] = confirmed_at
        result["last_confirmed_age_sec"] = (
            max(0.0, now - confirmed_at) if confirmed_at > 0.0 else None
        )
        return result

    def _persist(self, payload: dict[str, Any]) -> None:
        try:
            _atomic_write_json(self.state_path, payload)
        except Exception as exc:
            self._logger.warning(
                "Failed to persist local replica health state (%s): %s",
                self.state_path,
                exc,
            )
            return
        with self._lock:
            self._last_persisted_at = max(
                self._last_persisted_at,
                self._number(payload.get("updated_at"), default=0.0),
            )

    def _safe_now(self) -> float:
        return self._number(self._clock(), default=time.time())

    @staticmethod
    def _number(value: Any, *, default: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return float(default)
        return number if math.isfinite(number) else float(default)

    @classmethod
    def _timestamp(cls, value: Any, *, now: float) -> float:
        timestamp = cls._number(value, default=0.0)
        # A future wall-clock value cannot prove a current successful copy.
        if timestamp <= 0.0 or timestamp > now + 300.0:
            return 0.0
        return timestamp

    def _latest_confirmation_at(
        self,
        state: dict[str, Any],
        *,
        now: float,
    ) -> float:
        return max(
            self._timestamp(state.get("actual_copy_installed_at"), now=now),
            self._timestamp(state.get("last_unchanged_verified_at"), now=now),
        )

    def _capture_pending_diagnostic(self, details: dict[str, Any]) -> object:
        from rem_card.services.crash_reports import capture_crash_event

        return capture_crash_event(
            "local_replica_stale",
            role=self.role,
            details=details,
        )
