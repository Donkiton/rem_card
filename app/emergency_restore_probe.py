from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Any, Callable

from rem_card.app.db_runtime_context import DbRuntimeContext, build_network_runtime_context
from rem_card.app.emergency_metadata import (
    EmergencyMetadataError,
    EmergencySessionMetadata,
    atomic_write_json,
)
from rem_card.app.emergency_paths import (
    ACTIVE_DIR_NAME,
    resolve_emergency_root,
)
from rem_card.app.emergency_remote_identity import remote_identity_paths_match, validate_remote_identity_error
from rem_card.app.emergency_store import EmergencyLocalStore
from rem_card.app.emergency_validation import (
    SnapshotValidationResult,
    validate_medical_db_snapshot,
    validate_settings_db_snapshot,
)
from rem_card.app.local_metrics import record_metric
from rem_card.app.sqlite_shared import NETWORK_SAFE_DB_PROFILE
from rem_card.app.startup_db_guard import _compare_client_versions
from rem_card.app.version import APP_VERSION


logger = logging.getLogger("RemCard")


MERGE_READY_MARKER_FILE_NAME = "emergency_merge_ready.json"
EMERGENCY_MERGE_LOCK_FILE_NAME = "emergency_merge.lock"
RESTORE_PROBE_FILE_NAME = "emergency_restore_probe.tmp"
EMERGENCY_NURSE_ROLE_LOCK_FILE_NAME = "nurse_emergency.lock"
EMERGENCY_NURSE_ROLE_LOCK_STALE_TIMEOUT_SEC = 75.0
EMERGENCY_NURSE_ROLE_LOCK_HEARTBEAT_SEC = 8.0

MERGE_READY_MODE_A_MESSAGE = (
    "Доступ к сетевой базе восстановлен.\n\n"
    "Для объединения данных нужно закрыть RemCard.\n"
    "После закрытия аварийные изменения будут автоматически перенесены в сетевую базу.\n\n"
    "Не запускайте RemCard на других компьютерах до завершения объединения."
)

REMOTE_CHANGED_CONFLICT_MESSAGE = (
    "Доступ к сетевой базе восстановлен, но сетевая база изменилась после создания аварийной копии.\n\n"
    "Если выбрать объединение, локальные аварийные изменения этого ПК будут перенесены в сетевую медицинскую БД "
    "после обязательных резервных копий и проверок. При конфликте RemCard-строк победит локальная аварийная версия.\n"
    "БД настроек и данные оперблока на сетевой папке заменяться не будут."
)

DEFAULT_SUCCESS_ROUNDS_REQUIRED = max(1, int(os.environ.get("REMCARD_RESTORE_PROBE_SUCCESS_ROUNDS", "3")))
DEFAULT_RESTORE_PROBE_INTERVAL_SEC = max(
    10.0,
    float(os.environ.get("REMCARD_RESTORE_PROBE_INTERVAL_SEC", "15")),
)
DEFAULT_RESTORE_PROBE_FAILURE_BACKOFF_SEC = max(
    10.0,
    float(os.environ.get("REMCARD_RESTORE_PROBE_FAILURE_BACKOFF_SEC", "20")),
)
DEFAULT_RESTORE_PROBE_STABILITY_WINDOW_SEC = max(
    30.0,
    float(os.environ.get("REMCARD_RESTORE_PROBE_STABILITY_WINDOW_SEC", "60")),
)


@dataclass(frozen=True)
class EmergencyRestoreStatus:
    enabled: bool
    started: bool = False
    running: bool = False
    pending: bool = False
    status: str = "idle"
    reason: str = ""
    error: str = ""
    consecutive_successes: int = 0
    success_rounds_required: int = DEFAULT_SUCCESS_ROUNDS_REQUIRED
    network_stable: bool = False
    merge_ready: bool = False
    merge_mode: str = ""
    remote_last_change_id: int = 0
    base_last_change_id: int = 0
    remote_fingerprint: dict[str, Any] = field(default_factory=dict)
    remote_db_path: str = ""
    settings_db_path: str = ""
    emergency_session_id: str = ""
    merge_ready_marker_path: str = ""
    last_probe_ts: float = 0.0
    last_success_ts: float = 0.0
    first_success_ts: float = 0.0
    next_allowed_ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def merge_ready_marker_path(root: str | None, session_id: str) -> str:
    return os.path.join(resolve_emergency_root(root), ACTIVE_DIR_NAME, str(session_id), MERGE_READY_MARKER_FILE_NAME)


def emergency_merge_lock_path(baza_dir: str) -> str:
    return os.path.join(os.path.abspath(os.path.normpath(baza_dir)), "locks", EMERGENCY_MERGE_LOCK_FILE_NAME)


def clear_merge_ready_marker(root: str | None, session_id: str) -> bool:
    path = merge_ready_marker_path(root, session_id)
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        logger.warning("Failed to clear emergency merge-ready marker %s: %s", path, exc)
        return False


def write_merge_ready_marker(
    root: str | None,
    session: EmergencySessionMetadata,
    *,
    remote_last_change_id: int,
    remote_fingerprint: dict[str, Any],
    marker_mode: str = "mode_a_remote_unchanged",
) -> str:
    path = merge_ready_marker_path(root, session.emergency_session_id)
    payload = {
        "emergency_session_id": session.emergency_session_id,
        "requested_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": str(marker_mode or "mode_a_remote_unchanged"),
        "remote_last_change_id": int(remote_last_change_id or 0),
        "base_last_change_id": int(session.base_last_change_id or 0),
        "remote_fingerprint": dict(remote_fingerprint or {}),
        "source_role": "nurse",
        "source_client_id": str(session.source_client_id or ""),
        "app_version": APP_VERSION,
        "status": "merge_ready_requested",
    }
    atomic_write_json(path, payload)
    return path


class EmergencyRestoreProbe:
    def __init__(
        self,
        *,
        role: str | None,
        runtime_context: DbRuntimeContext | None,
        store: EmergencyLocalStore | None = None,
        session_metadata: EmergencySessionMetadata | None = None,
        network_context_factory: Callable[[], DbRuntimeContext] | None = None,
        is_shutdown: Callable[[], bool] | None = None,
        is_local_write_idle: Callable[[], bool] | None = None,
        is_local_maintenance_idle: Callable[[], bool] | None = None,
        success_rounds_required: int = DEFAULT_SUCCESS_ROUNDS_REQUIRED,
        stability_window_sec: float = DEFAULT_RESTORE_PROBE_STABILITY_WINDOW_SEC,
        source_medical_db_path: str | None = None,
        source_settings_db_path: str | None = None,
        network_baza_dir: str | None = None,
    ):
        self.role = str(role or "").strip().lower()
        self.runtime_context = runtime_context
        self.store = store or EmergencyLocalStore(root=_infer_emergency_root(runtime_context))
        self.session_metadata = session_metadata
        self.network_context_factory = network_context_factory or build_network_runtime_context
        self.is_shutdown = is_shutdown or (lambda: False)
        self.is_local_write_idle = is_local_write_idle or (lambda: True)
        self.is_local_maintenance_idle = is_local_maintenance_idle or (lambda: True)
        self.success_rounds_required = max(1, int(success_rounds_required or 1))
        self.stability_window_sec = max(1.0, float(stability_window_sec or 1.0))
        self.source_medical_db_path = _optional_path(source_medical_db_path)
        self.source_settings_db_path = _optional_path(source_settings_db_path)
        self.network_baza_dir = _optional_path(network_baza_dir)
        self._lock = threading.Lock()
        self._network_emergency_role_lock = None
        self._status = EmergencyRestoreStatus(
            enabled=self.enabled,
            success_rounds_required=self.success_rounds_required,
        )

    @staticmethod
    def is_enabled_for_runtime(role: str | None, mode: str | None) -> bool:
        normalized_role = str(role or "").strip().lower()
        return normalized_role in {"doctor", "nurse"} and str(mode or "").strip().lower() == "emergency"

    @property
    def enabled(self) -> bool:
        mode = getattr(self.runtime_context, "mode", "")
        return self.is_enabled_for_runtime(self.role, mode)

    def run_probe_once(self) -> dict[str, Any]:
        with self._lock:
            self._status = replace(
                self._status,
                enabled=self.enabled,
                running=True,
                status="running",
                reason="probe_started",
                error="",
                last_probe_ts=time.time(),
            )
        try:
            status = self._run_probe_round()
        except Exception as exc:
            logger.warning("Emergency restore probe failed: %s", exc, exc_info=True)
            status = self._failure("exception", str(exc))
        with self._lock:
            self._status = replace(status, enabled=self.enabled, running=False)
            return self._status.to_dict()

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return replace(self._status, enabled=self.enabled).to_dict()

    def is_network_stable(self) -> bool:
        status = self.get_status()
        return bool(status.get("network_stable"))

    def mark_merge_ready(self) -> str:
        session = self._load_session()
        status = self.get_status()
        status_name = str(status.get("status") or "")
        can_merge = (
            (status_name == "merge_ready_mode_a" and bool(status.get("merge_ready")))
            or (status_name == "remote_changed_conflict_pending" and bool(status.get("network_stable")))
        )
        if not can_merge:
            raise RuntimeError("Сетевая база еще не готова к закрытию для объединения")
        marker_path = self.write_merge_ready_marker()
        self.store.mark_session_status(session.emergency_session_id, "merge_pending")
        with self._lock:
            self._status = replace(
                self._status,
                status="merge_ready_requested",
                reason="user_requested_close_for_merge",
                merge_ready_marker_path=marker_path,
            )
        record_metric("emergency_restore_merge_ready_requested", 1, session_id=session.emergency_session_id)
        return marker_path

    def write_merge_ready_marker(self) -> str:
        session = self._load_session()
        status = self.get_status()
        merge_mode = str(status.get("merge_mode") or "")
        marker_mode = {
            "mode_a_remote_unchanged": "mode_a_remote_unchanged",
            "remote_changed_conflict_pending": "remote_changed_emergency_authoritative",
        }.get(merge_mode)
        if marker_mode is None:
            raise RuntimeError("Merge-ready marker разрешен только после стабильного восстановления сети")
        return write_merge_ready_marker(
            self.store.resolve_root(),
            session,
            remote_last_change_id=int(status.get("remote_last_change_id") or 0),
            remote_fingerprint=dict(status.get("remote_fingerprint") or {}),
            marker_mode=marker_mode,
        )

    def clear_merge_ready_marker(self) -> bool:
        session = self._load_session()
        return clear_merge_ready_marker(self.store.resolve_root(), session.emergency_session_id)

    def _run_probe_round(self) -> EmergencyRestoreStatus:
        guard_failure = self._runtime_guard_failure()
        if guard_failure:
            return guard_failure
        session = self._load_session()
        if session.status not in {"active", "merge_failed"}:
            return self._failure(
                "active_emergency_session_required",
                f"session status is {session.status}",
                session=session,
            )
        local_validation = validate_medical_db_snapshot(session.local_db_path)
        if not local_validation.ok:
            return self._failure("local_emergency_db_invalid", local_validation.reason, session=session)

        context = self._network_context()
        path_failure = self._check_network_paths(context)
        if path_failure:
            return self._failure(path_failure[0], path_failure[1], session=session)
        self._ensure_network_emergency_role_marker(context, session)

        medical_validation = validate_medical_db_snapshot(context.medical_db_path)
        settings_validation = validate_settings_db_snapshot(context.settings_db_path)
        validation_failure = self._validation_failure(medical_validation, settings_validation)
        if validation_failure:
            return self._failure(validation_failure[0], validation_failure[1], session=session)

        readiness_failure = self._check_remote_readiness(context, session, medical_validation)
        if readiness_failure:
            return self._failure(readiness_failure[0], readiness_failure[1], session=session)

        return self._record_remote_change_classification(session, medical_validation, context)

    def _ensure_network_emergency_role_marker(
        self,
        context: DbRuntimeContext,
        session: EmergencySessionMetadata,
    ) -> None:
        if self.role != "nurse":
            return
        lock_path = os.path.join(context.session_locks_dir, EMERGENCY_NURSE_ROLE_LOCK_FILE_NAME)
        current_lock = self._network_emergency_role_lock
        if current_lock is not None and getattr(current_lock, "lock_path", "") == lock_path:
            try:
                if current_lock.refresh():
                    return
            except Exception as exc:
                logger.warning("Failed to refresh network emergency nurse marker %s: %s", lock_path, exc)
            self.release_network_emergency_role_marker()
        if current_lock is not None:
            self.release_network_emergency_role_marker()
        try:
            from rem_card.app.role_session_lock import RoleSessionLock

            owner_parts = [
                socket.gethostname(),
                str(os.getpid()),
                "nurse_emergency",
                str(session.emergency_session_id or ""),
            ]
            role_lock = RoleSessionLock(
                lock_path=lock_path,
                role="nurse_emergency",
                owner_id=":".join(owner_parts),
                stale_timeout_sec=EMERGENCY_NURSE_ROLE_LOCK_STALE_TIMEOUT_SEC,
                heartbeat_sec=EMERGENCY_NURSE_ROLE_LOCK_HEARTBEAT_SEC,
                logger=logger,
            )
            if role_lock.acquire():
                self._network_emergency_role_lock = role_lock
            else:
                logger.warning(
                    "Failed to acquire network emergency nurse marker %s: %s",
                    lock_path,
                    role_lock.describe_holder(),
                )
        except Exception as exc:
            logger.warning("Failed to create network emergency nurse marker %s: %s", lock_path, exc, exc_info=True)

    def release_network_emergency_role_marker(self) -> None:
        role_lock = self._network_emergency_role_lock
        self._network_emergency_role_lock = None
        if role_lock is None:
            return
        try:
            role_lock.release()
        except Exception as exc:
            logger.warning("Failed to release network emergency nurse marker: %s", exc, exc_info=True)

    def _runtime_guard_failure(self) -> EmergencyRestoreStatus | None:
        if not self.enabled:
            return self._failure("disabled", f"role={self.role or '-'} mode={getattr(self.runtime_context, 'mode', '-')}")
        if bool(self.is_shutdown()):
            return self._failure("shutdown", "application is shutting down")
        try:
            if not bool(self.is_local_write_idle()):
                return self._failure("local_write_busy", "local write queue is busy")
            if not bool(self.is_local_maintenance_idle()):
                return self._failure("local_maintenance_busy", "local backup or quick_check is running")
        except Exception as exc:
            return self._failure("local_state_unavailable", str(exc))
        return None

    def _load_session(self) -> EmergencySessionMetadata:
        if self.session_metadata is not None:
            metadata = self.store.read_active_session(self.session_metadata.emergency_session_id)
            self.session_metadata = metadata
            return metadata
        session_id = str(getattr(self.runtime_context, "emergency_session_id", "") or "").strip()
        if not session_id:
            raise EmergencyMetadataError("Emergency runtime context does not contain session id")
        metadata = self.store.read_active_session(session_id)
        self.session_metadata = metadata
        return metadata

    def _network_context(self) -> DbRuntimeContext:
        context = self.network_context_factory()
        if not any((self.network_baza_dir, self.source_medical_db_path, self.source_settings_db_path)):
            return context
        return _context_with_overrides(
            context,
            baza_dir=self.network_baza_dir,
            medical_db_path=self.source_medical_db_path,
            settings_db_path=self.source_settings_db_path,
        )

    def _check_network_paths(self, context: DbRuntimeContext) -> tuple[str, str] | None:
        for label, path in (
            ("network_baza_unavailable", context.baza_dir),
            ("network_medical_db_missing", context.medical_db_path),
            ("network_settings_db_missing", context.settings_db_path),
        ):
            if not os.path.exists(path):
                return label, f"required path is unavailable: {path}"
        if not os.path.isdir(context.baza_dir):
            return "network_baza_unavailable", f"network BAZA_DIR is not a directory: {context.baza_dir}"
        return None

    def _validation_failure(
        self,
        medical_validation: SnapshotValidationResult,
        settings_validation: SnapshotValidationResult,
    ) -> tuple[str, str] | None:
        if not medical_validation.ok:
            return _remote_failure_status("network_medical_db_unavailable", medical_validation.reason)
        if not settings_validation.ok:
            return _remote_failure_status("network_settings_db_unavailable", settings_validation.reason)
        return None

    def _check_remote_readiness(
        self,
        context: DbRuntimeContext,
        session: EmergencySessionMetadata,
        medical_validation: SnapshotValidationResult,
    ) -> tuple[str, str] | None:
        checks = (
            self._check_schema_compatible(session, medical_validation),
            self._check_client_policy(context.medical_client_policy_path),
            self._check_remote_identity(session, context.medical_db_path, medical_validation),
            self._check_locks(context),
            self._check_probe_file(context),
        )
        return next((check for check in checks if check), None)

    def _check_schema_compatible(
        self,
        session: EmergencySessionMetadata,
        medical_validation: SnapshotValidationResult,
    ) -> tuple[str, str] | None:
        base_schema = int(session.schema_version or 0)
        remote_schema = int(medical_validation.schema_version or 0)
        if base_schema and remote_schema != base_schema:
            return "schema_incompatible", f"remote schema={remote_schema}, base schema={base_schema}"
        return None

    def _check_client_policy(self, policy_path: str) -> tuple[str, str] | None:
        try:
            payload = _read_json_dict(policy_path)
        except Exception as exc:
            return "client_policy_unavailable", str(exc)
        min_version = str(payload.get("min_client_version") or APP_VERSION)
        if _compare_client_versions(APP_VERSION, min_version) < 0:
            return "client_policy_block", "client version is below min_client_version"
        if str(payload.get("required_db_profile") or "").strip() != NETWORK_SAFE_DB_PROFILE:
            return "client_policy_block", "required_db_profile is incompatible"
        if bool(payload.get("wal_allowed_on_shared_db")):
            return "client_policy_block", "WAL is not allowed on shared DB"
        return None

    def _check_remote_identity(
        self,
        session: EmergencySessionMetadata,
        remote_path: str,
        medical_validation: SnapshotValidationResult,
    ) -> tuple[str, str] | None:
        error = validate_remote_identity_error(session, remote_path, medical_validation)
        if error:
            return "remote_identity_mismatch", error
        return None

    def _check_locks(self, context: DbRuntimeContext) -> tuple[str, str] | None:
        db_lock_failure = _probe_lock_file_available(context.medical_db_lock_path)
        if db_lock_failure:
            return "db_lock_active", db_lock_failure
        merge_lock = emergency_merge_lock_path(context.baza_dir)
        if os.path.exists(merge_lock):
            return "emergency_merge_lock_active", merge_lock
        active_lock = _first_existing_lock(
            context.session_locks_dir,
            (
                "doctor.lock",
                "nurse.lock",
                "operblock.lock",
                "operblock_emergency.lock",
                "operblock_planned.lock",
            ),
        )
        if active_lock:
            return "session_lock_active", active_lock
        return None

    def _check_probe_file(self, context: DbRuntimeContext) -> tuple[str, str] | None:
        directory = context.medical_backup_health_dir
        if not os.path.isdir(directory):
            return "probe_file_unavailable", f"probe directory is unavailable: {directory}"
        path = os.path.join(directory, RESTORE_PROBE_FILE_NAME)
        token = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, token.encode("utf-8"))
            finally:
                os.close(fd)
            os.remove(path)
        except FileExistsError:
            return "probe_file_unavailable", f"probe file already exists: {path}"
        except OSError as exc:
            return "probe_file_unavailable", str(exc)
        return None

    def _record_remote_change_classification(
        self,
        session: EmergencySessionMetadata,
        medical_validation: SnapshotValidationResult,
        context: DbRuntimeContext,
    ) -> EmergencyRestoreStatus:
        remote_last = int(medical_validation.last_change_id or 0)
        base_last = int(session.base_last_change_id or 0)
        if remote_last < base_last:
            return self._failure(
                "remote_inconsistent",
                f"remote_last_change_id={remote_last} < base_last_change_id={base_last}",
                session=session,
                remote_validation=medical_validation,
                context=context,
            )
        if remote_last == base_last:
            return self._record_success(
                "merge_ready_mode_a",
                "mode_a_remote_unchanged",
                session,
                medical_validation,
                context,
            )
        return self._record_success(
            "remote_changed_conflict_pending",
            "remote_changed_conflict_pending",
            session,
            medical_validation,
            context,
        )

    def _record_success(
        self,
        final_status: str,
        merge_mode: str,
        session: EmergencySessionMetadata,
        medical_validation: SnapshotValidationResult,
        context: DbRuntimeContext,
    ) -> EmergencyRestoreStatus:
        now = time.time()
        current = self.get_status()
        first_ts = float(current.get("first_success_ts") or 0.0) or now
        consecutive = int(current.get("consecutive_successes") or 0) + 1
        stable = consecutive >= self.success_rounds_required and (now - first_ts) <= self.stability_window_sec
        status = final_status if stable else f"round_success_{final_status}"
        if not stable and (now - first_ts) > self.stability_window_sec:
            consecutive = 1
            first_ts = now
        record_metric(
            "emergency_restore_probe_success",
            1,
            status=status,
            consecutive_successes=consecutive,
            remote_last_change_id=int(medical_validation.last_change_id or 0),
        )
        return EmergencyRestoreStatus(
            enabled=self.enabled,
            started=True,
            running=False,
            status=status,
            reason="ok",
            consecutive_successes=consecutive,
            success_rounds_required=self.success_rounds_required,
            network_stable=stable,
            merge_ready=stable and final_status == "merge_ready_mode_a",
            merge_mode=merge_mode if stable else "",
            remote_last_change_id=int(medical_validation.last_change_id or 0),
            base_last_change_id=int(session.base_last_change_id or 0),
            remote_fingerprint=dict(medical_validation.fingerprint or {}),
            remote_db_path=context.medical_db_path,
            settings_db_path=context.settings_db_path,
            emergency_session_id=session.emergency_session_id,
            merge_ready_marker_path=merge_ready_marker_path(self.store.resolve_root(), session.emergency_session_id),
            last_probe_ts=now,
            last_success_ts=now,
            first_success_ts=first_ts,
        )

    def _failure(
        self,
        status: str,
        reason: str,
        *,
        session: EmergencySessionMetadata | None = None,
        remote_validation: SnapshotValidationResult | None = None,
        context: DbRuntimeContext | None = None,
    ) -> EmergencyRestoreStatus:
        record_metric("emergency_restore_probe_failed", 1, status=status, reason=reason)
        return EmergencyRestoreStatus(
            enabled=self.enabled,
            started=True,
            status=status,
            reason=str(reason or ""),
            error=str(reason or ""),
            consecutive_successes=0,
            success_rounds_required=self.success_rounds_required,
            network_stable=False,
            merge_ready=False,
            remote_last_change_id=int(getattr(remote_validation, "last_change_id", 0) or 0),
            base_last_change_id=int(getattr(session, "base_last_change_id", 0) or 0),
            remote_fingerprint=dict(getattr(remote_validation, "fingerprint", {}) or {}),
            remote_db_path=str(getattr(context, "medical_db_path", "") or ""),
            settings_db_path=str(getattr(context, "settings_db_path", "") or ""),
            emergency_session_id=str(getattr(session, "emergency_session_id", "") or ""),
            merge_ready_marker_path=(
                merge_ready_marker_path(self.store.resolve_root(), session.emergency_session_id) if session else ""
            ),
            last_probe_ts=time.time(),
        )


class EmergencyRestoreProbeScheduler:
    def __init__(
        self,
        probe: EmergencyRestoreProbe,
        *,
        interval_sec: float = DEFAULT_RESTORE_PROBE_INTERVAL_SEC,
        failure_backoff_sec: float = DEFAULT_RESTORE_PROBE_FAILURE_BACKOFF_SEC,
        on_status: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.probe = probe
        self.interval_sec = max(1.0, float(interval_sec or 1.0))
        self.failure_backoff_sec = max(1.0, float(failure_backoff_sec or 1.0))
        self.on_status = on_status
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False
        self._running = False
        self._pending = False
        self._next_allowed_ts = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.probe.enabled)

    def start(self) -> bool:
        if not self.enabled:
            self._emit_status({"status": "disabled", "reason": "runtime_not_supported"})
            return False
        with self._lock:
            if self._started:
                return True
            self._started = True
            self._stop_event.clear()
            self._wake_event.clear()
            self._thread = threading.Thread(target=self._loop, name="EmergencyRestoreProbe", daemon=True)
            self._thread.start()
        self.request_probe("startup")
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        with self._lock:
            self._started = False
            thread = self._thread
        self._stop_event.set()
        self._wake_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout or 0.0)))
        self.probe.release_network_emergency_role_marker()
        with self._lock:
            stopped = not self._running
            return stopped

    def request_probe(self, reason: str = "manual") -> bool:
        with self._lock:
            if not self._started or self._stop_event.is_set():
                return False
            self._pending = True
        self._wake_event.set()
        return True

    def run_probe_once(self) -> dict[str, Any]:
        return self.probe.run_probe_once()

    def get_status(self) -> dict[str, Any]:
        status = self.probe.get_status()
        with self._lock:
            status.update(
                {
                    "started": bool(self._started),
                    "running": bool(self._running),
                    "pending": bool(self._pending),
                    "next_allowed_ts": float(self._next_allowed_ts),
                }
            )
        return status

    def is_network_stable(self) -> bool:
        return self.probe.is_network_stable()

    def mark_merge_ready(self) -> str:
        return self.probe.mark_merge_ready()

    def write_merge_ready_marker(self) -> str:
        return self.probe.write_merge_ready_marker()

    def clear_merge_ready_marker(self) -> bool:
        return self.probe.clear_merge_ready_marker()

    def _loop(self) -> None:
        self._next_allowed_ts = time.monotonic()
        while not self._stop_event.is_set():
            wait_sec = max(0.1, self._next_allowed_ts - time.monotonic())
            self._wake_event.wait(wait_sec)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            if time.monotonic() < self._next_allowed_ts:
                continue
            self._run_scheduled_probe()

    def _run_scheduled_probe(self) -> None:
        with self._lock:
            if self._running or not self._started:
                self._pending = True
                return
            self._running = True
            self._pending = False
        try:
            status = self.probe.run_probe_once()
            self._emit_status(status)
            delay = self.interval_sec if _status_is_success_like(status) else self.failure_backoff_sec
            with self._lock:
                self._next_allowed_ts = time.monotonic() + delay
        finally:
            with self._lock:
                self._running = False

    def _emit_status(self, status: dict[str, Any]) -> None:
        if self._stop_event.is_set() or not self.on_status:
            return
        try:
            self.on_status(dict(status or {}))
        except Exception as exc:
            logger.warning("Emergency restore probe status callback failed: %s", exc, exc_info=True)


def create_emergency_restore_probe_scheduler_for_runtime(
    *,
    role: str | None,
    runtime_context: DbRuntimeContext | None,
    is_local_write_idle: Callable[[], bool] | None = None,
    is_shutdown: Callable[[], bool] | None = None,
    on_status: Callable[[dict[str, Any]], None] | None = None,
) -> EmergencyRestoreProbeScheduler | None:
    if not EmergencyRestoreProbe.is_enabled_for_runtime(role, getattr(runtime_context, "mode", None)):
        return None
    probe = EmergencyRestoreProbe(
        role=role,
        runtime_context=runtime_context,
        is_local_write_idle=is_local_write_idle,
        is_shutdown=is_shutdown,
    )
    return EmergencyRestoreProbeScheduler(probe, on_status=on_status)


def _infer_emergency_root(runtime_context: DbRuntimeContext | None) -> str | None:
    baza_dir = str(getattr(runtime_context, "baza_dir", "") or "")
    if not baza_dir:
        return None
    parent = os.path.dirname(os.path.abspath(os.path.normpath(baza_dir)))
    if os.path.basename(parent).lower() == ACTIVE_DIR_NAME:
        return os.path.dirname(parent)
    return None


def _optional_path(path: str | None) -> str | None:
    if not path:
        return None
    return os.path.abspath(os.path.normpath(str(path)))


def _context_with_overrides(
    context: DbRuntimeContext,
    *,
    baza_dir: str | None,
    medical_db_path: str | None,
    settings_db_path: str | None,
) -> DbRuntimeContext:
    from dataclasses import replace as dataclass_replace

    effective_baza = _optional_path(baza_dir) or context.baza_dir
    effective_medical = _optional_path(medical_db_path) or context.medical_db_path
    effective_settings = _optional_path(settings_db_path) or context.settings_db_path
    return dataclass_replace(
        context,
        baza_dir=effective_baza,
        medical_db_path=effective_medical,
        medical_db_lock_path=os.path.join(os.path.dirname(effective_medical), "db.lock"),
        medical_backup_health_dir=os.path.join(effective_baza, "backup_health"),
        session_locks_dir=os.path.join(effective_baza, "session_locks"),
        settings_db_path=effective_settings,
        settings_db_lock_path=os.path.join(os.path.dirname(effective_settings), "settings.db.lock"),
        settings_backup_health_dir=os.path.join(os.path.dirname(effective_settings), "backup_health"),
        medical_client_policy_path=os.path.join(effective_baza, "config", "client_policy.json"),
    )


def _remote_failure_status(default_status: str, reason: str) -> tuple[str, str]:
    text = str(reason or "").lower()
    if "locked" in text or "busy" in text or "занят" in text or "заблок" in text:
        return "locked_busy", reason
    if "schema" in text or "схем" in text or "migration" in text:
        return "schema_incompatible", reason
    if "client_policy" in text or "min_client_version" in text:
        return "client_policy_block", reason
    return default_status, reason


def _read_json_dict(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise RuntimeError(f"JSON policy is not an object: {path}")
    return payload


def _path_identity_compatible(base_path: str, remote_path: str) -> bool:
    return remote_identity_paths_match(base_path, remote_path)


def _probe_lock_file_available(lock_path: str) -> str:
    directory = os.path.dirname(lock_path)
    if not os.path.isdir(directory):
        return f"lock directory is unavailable: {directory}"
    payload = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        os.remove(lock_path)
        return ""
    except FileExistsError:
        return f"db.lock is active: {lock_path}"
    except OSError as exc:
        return str(exc)


def _first_existing_lock(directory: str, names: tuple[str, ...]) -> str:
    if not os.path.isdir(directory):
        return ""
    for name in names:
        path = os.path.join(directory, name)
        if os.path.exists(path):
            return path
    return ""


def _status_is_success_like(status: dict[str, Any]) -> bool:
    text = str((status or {}).get("status") or "")
    return text.startswith("round_success_") or text in {"merge_ready_mode_a", "remote_changed_conflict_pending"}
