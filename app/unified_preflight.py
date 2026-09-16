"""Startup preflight shared by the unified shell and the legacy entrypoints.

The unified shell acquires :class:`SessionLease` before it calls this module.
That lease is the maintenance admission proof and replaces the legacy
``RoleSessionLock`` for an admitted unified role.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from typing import Any, Callable


_STARTUP_REQUEST_ATTRIBUTE = "_remcard_unified_startup_request"
_EMERGENCY_DISPATCHED_ATTRIBUTE = "_remcard_unified_emergency_dispatched"
_EMERGENCY_ROLES = frozenset({"doctor", "nurse"})
_OPERBLOCK_ROLES = frozenset({"operblock", "operblock_emergency", "operblock_planned"})
_LOCAL_ONLY_MODES = frozenset({"emergency", "opblock_offline"})
LOCAL_ONLY_STATE_PROPERTY = "remcard_local_only_runtime"
LOCAL_ONLY_ENV = "REMCARD_UNIFIED_LOCAL_ONLY"
CENTRAL_FAILURE_UNREACHABLE = "unreachable"
_LOCAL_ONLY_BOOTSTRAP_LOCK = threading.RLock()


class LocalOnlyStartupError(RuntimeError):
    def __init__(self, message: str, *, status: str):
        super().__init__(message)
        self.status = str(status)


class LocalOnlyStartupCancelled(LocalOnlyStartupError):
    pass


class LocalOnlyRestartRequired(LocalOnlyStartupError):
    def __init__(self, message: str, *, local_root: str, loaded_roots: dict[str, str]):
        super().__init__(message, status="local_restart_required")
        self.local_root = str(local_root)
        self.loaded_roots = dict(loaded_roots)


@dataclass(frozen=True)
class LocalOnlyRuntimeAdmission:
    role: str
    central_root: str
    local_root: str
    runtime_context: Any
    local_lease: Any

    @property
    def mode(self) -> str:
        return str(getattr(self.runtime_context, "mode", "") or "")

    @property
    def runtime_state(self) -> dict[str, Any]:
        return {
            "active": True,
            "mode": self.mode,
            "role": self.role,
            "central_root": self.central_root,
            "local_root": self.local_root,
            "central_access_allowed": False,
            "central_reacquire_required": True,
            "restore_probe_enabled": False,
            "pending_merge_enabled": False,
            "process_restart_required_before_central": True,
            "fresh_runtime_required_after_drain": True,
        }


@dataclass(frozen=True)
class UnifiedStartupRequest:
    """Validated hints retained until the admitted role finishes preflight."""

    role: str | None = None
    emergency_startup_request: str = ""
    resume_role: str | None = None

    @property
    def emergency_role(self) -> str | None:
        if self.resume_role in _EMERGENCY_ROLES:
            return self.resume_role
        if self.emergency_startup_request and self.role in _EMERGENCY_ROLES:
            return self.role
        return None


def build_startup_request(
    *,
    role: str | None = None,
    emergency_startup_request: str | None = None,
    resume_role: str | None = None,
) -> UnifiedStartupRequest:
    role_key = str(role or "").strip().casefold() or None
    marker_path = str(emergency_startup_request or "").strip()
    if marker_path and role_key not in _EMERGENCY_ROLES:
        raise ValueError("Emergency startup request requires the doctor or nurse role")
    if resume_role is not None and resume_role not in _EMERGENCY_ROLES:
        raise ValueError("Unsupported resume role")
    return UnifiedStartupRequest(role=role_key, emergency_startup_request=marker_path, resume_role=resume_role)


def attach_startup_request(shell: Any, request: UnifiedStartupRequest) -> None:
    """Attach CLI hints without making an ordinary ``--role`` skip the chooser."""

    setattr(shell, _STARTUP_REQUEST_ATTRIBUTE, request)
    setattr(shell, _EMERGENCY_DISPATCHED_ATTRIBUTE, False)


def get_startup_request(shell: Any) -> UnifiedStartupRequest:
    request = getattr(shell, _STARTUP_REQUEST_ATTRIBUTE, None)
    return request if isinstance(request, UnifiedStartupRequest) else UnifiedStartupRequest()


def complete_startup_request(shell: Any) -> None:
    """Forget one-shot restart data after the admitted container is ready."""

    setattr(shell, _STARTUP_REQUEST_ATTRIBUTE, UnifiedStartupRequest())
    setattr(shell, _EMERGENCY_DISPATCHED_ATTRIBUTE, True)


def take_emergency_role_after_chooser_ready(shell: Any) -> str | None:
    """Return the one-shot emergency role only after the chooser is ready.

    ``UnifiedWindow._ready`` should call this after displaying the chooser and
    refreshing maintenance access, then pass a non-empty result to
    ``enter_role``.  A plain legacy ``--role`` remains only a hint and returns
    ``None`` here.
    """

    if bool(getattr(shell, _EMERGENCY_DISPATCHED_ATTRIBUTE, False)):
        return None
    role = get_startup_request(shell).emergency_role
    if role is None:
        return None
    setattr(shell, _EMERGENCY_DISPATCHED_ATTRIBUTE, True)
    return role


def _require_central_admission(central_lease: Any, role: str) -> None:
    if central_lease is None or not bool(getattr(central_lease, "held", False)):
        raise RuntimeError("Unified startup requires an active central SessionLease")
    lease_role = str(getattr(central_lease, "role", "") or "").strip().casefold()
    if lease_role != role:
        raise RuntimeError("Central SessionLease role does not match the admitted role")
    store = getattr(central_lease, "store", None)
    lease_root = getattr(store, "root", None)
    if lease_root is None:
        raise RuntimeError("Central SessionLease does not identify its maintenance root")
    from rem_card.app.runtime_paths import resolve_baza_dir

    if not _same_path(lease_root, resolve_baza_dir()):
        raise RuntimeError("Central SessionLease does not protect the configured database root")


def prepare_admitted_runtime_context(
    *,
    role: str,
    central_lease: Any,
    emergency_startup_request: str | None = None,
    before_user_message: Callable[[], None] | None = None,
):
    """Run guarded runtime selection for an already admitted unified role.

    The caller must run this on the GUI thread because existing emergency
    startup decisions can show dialogs.  The central lease stays owned by the
    shell; the legacy role lock is deliberately not acquired.
    """

    role_key = str(role or "").strip().casefold()
    _require_central_admission(central_lease, role_key)
    request = build_startup_request(
        role=role_key,
        emergency_startup_request=emergency_startup_request,
    )

    from rem_card.app import main as legacy_startup

    active_local_case = legacy_startup._has_active_local_operblock_case_before_network_probe(role_key)
    close_callback = before_user_message if callable(before_user_message) else (lambda: None)
    runtime_context, _reason, role_lock = legacy_startup._prepare_runtime_context_for_startup(
        SimpleNamespace(
            role=role_key,
            emergency_startup_request=request.emergency_startup_request,
        ),
        active_local_case,
        SimpleNamespace(close=close_callback),
        acquire_role_lock=False,
    )
    if role_lock is not None:  # Defensive contract check; never release an unknown lock here.
        raise RuntimeError("Unified startup unexpectedly acquired a legacy role lock")
    return runtime_context


def bootstrap_admitted_container(
    bootstrap: Callable[..., Any],
    *,
    role: str,
    central_lease: Any,
    runtime_context: Any,
    emergency_startup_request: str | None = None,
    before_user_message: Callable[[], None] | None = None,
) -> tuple[Any, Any]:
    """Bootstrap exactly the context selected under central admission.

    A bootstrap error must return to the shell with its partial-owner metadata.
    Starting a second fallback runtime here would lose the admission boundary
    and can leave the first container alive.
    """

    role_key = str(role or "").strip().casefold()
    _require_central_admission(central_lease, role_key)
    build_startup_request(
        role=role_key,
        emergency_startup_request=emergency_startup_request,
    )
    _ = before_user_message  # Kept for source-compatible shell call sites.
    bootstrap_role = None if role_key == "settings" else role_key
    container = bootstrap(role=bootstrap_role, runtime_context=runtime_context)
    return container, runtime_context


_LOCAL_CONTEXT_PATH_FIELDS = (
    "baza_dir",
    "medical_db_path",
    "medical_db_lock_path",
    "medical_backups_valid_dir",
    "medical_backup_health_dir",
    "medical_quarantine_dir",
    "medical_snapshots_dir",
    "medical_logs_dir",
    "recovery_lock_path",
    "session_locks_dir",
    "settings_db_path",
    "settings_db_lock_path",
    "settings_backups_dir",
    "settings_backup_health_dir",
    "medical_backups_root_dir",
    "medical_invalid_backups_dir",
    "medical_db_rotation_lock_path",
    "medical_client_policy_path",
    "medical_startup_quickcheck_state_path",
)


def _normalized_path(path: str | os.PathLike[str]) -> str:
    # Path canonicalizes a UNC share root's trailing separator without I/O.
    return str(Path(os.path.abspath(os.path.normpath(str(path)))))


def _same_path(first: str | os.PathLike[str], second: str | os.PathLike[str]) -> bool:
    return os.path.normcase(_normalized_path(first)) == os.path.normcase(_normalized_path(second))


def _central_medical_db_path(central_root: str) -> str:
    return _normalized_path(os.path.join(central_root, "archiv", "rao_journal.db"))


def _require_emergency_source_identity(decision: Any, central_root: str) -> None:
    """Bind a local snapshot to the selected central root without reading it."""

    metadata = getattr(decision, "active_session_metadata", None)
    source_path = str(getattr(metadata, "base_remote_db_path", "") or "").strip()
    if metadata is None:
        metadata = getattr(decision, "standby_metadata", None)
        source_path = str(getattr(metadata, "source_remote_db_path", "") or "").strip()
    if not source_path or not _same_path(source_path, _central_medical_db_path(central_root)):
        raise LocalOnlyStartupError(
            "Локальная аварийная копия относится к другой основной базе.",
            status="local_snapshot_central_mismatch",
        )


def _path_is_within(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    candidate = os.path.normcase(os.path.realpath(os.path.abspath(str(path))))
    boundary = os.path.normcase(os.path.realpath(os.path.abspath(str(root))))
    try:
        return os.path.commonpath((candidate, boundary)) == boundary
    except ValueError:
        return False


def _require_local_fixed_root(root: str) -> str:
    normalized = _normalized_path(root)
    if normalized.startswith("\\\\"):
        raise LocalOnlyStartupError(
            "Локальный аварийный каталог не может находиться в сетевой папке.",
            status="local_root_not_local",
        )
    if os.name == "nt":
        import ctypes

        drive, _tail = os.path.splitdrive(normalized)
        drive_root = drive + "\\" if drive else normalized
        drive_type = int(ctypes.windll.kernel32.GetDriveTypeW(str(drive_root)))
        if drive_type not in {3, 6}:  # DRIVE_FIXED or DRIVE_RAMDISK
            raise LocalOnlyStartupError(
                "Аварийный каталог должен находиться на локальном диске этого ПК.",
                status="local_root_not_local",
            )
    return normalized


def _loaded_static_baza_roots() -> dict[str, str]:
    loaded: dict[str, str] = {}
    for module_name in ("rem_card.app.paths", "app.paths"):
        module = sys.modules.get(module_name)
        if module is None:
            continue
        loaded[module_name] = str(getattr(module, "BAZA_DIR", "") or "")
    return loaded


def require_fresh_local_only_import_state(*, role: str, local_root: str | None = None) -> str:
    """Fail closed when path constants were already bound to another root.

    Many clinical modules use ``from rem_card.app.paths import ...``.  Updating
    the environment cannot update those copied values, so changing roots inside
    that process is unsafe.  The shell must drain, restore its environment and
    relaunch to the chooser before retrying local-only startup.
    """

    role_key = str(role or "").strip().casefold()
    if role_key in _EMERGENCY_ROLES:
        from rem_card.app.emergency_paths import resolve_emergency_root

        resolved_local_root = resolve_emergency_root(local_root)
    elif role_key in _OPERBLOCK_ROLES:
        from rem_card.app.operblock_offline_store import get_operblock_offline_root

        resolved_local_root = local_root or get_operblock_offline_root()
    else:
        raise LocalOnlyStartupError(
            "Для выбранной роли локальный аварийный запуск не поддерживается.",
            status="unsupported_local_role",
        )
    normalized_local_root = _require_local_fixed_root(resolved_local_root)
    mismatches = {
        name: value
        for name, value in _loaded_static_baza_roots().items()
        if not value or not _same_path(value, normalized_local_root)
    }
    if mismatches:
        raise LocalOnlyRestartRequired(
            "Для безопасного локального запуска RemCard необходимо перезапустить.",
            local_root=normalized_local_root,
            loaded_roots=mismatches,
        )
    return normalized_local_root


def _validate_local_runtime_context(*, role: str, runtime_context: Any, local_root: str) -> None:
    mode = str(getattr(runtime_context, "mode", "") or "").strip().casefold()
    if mode not in _LOCAL_ONLY_MODES or bool(getattr(runtime_context, "is_network", True)):
        raise LocalOnlyStartupError(
            "Для локального запуска передан недопустимый runtime context.",
            status="invalid_local_runtime_mode",
        )
    if mode == "emergency" and role not in _EMERGENCY_ROLES:
        raise LocalOnlyStartupError(
            "Аварийная база доступна только рабочим местам врача и медсестры.",
            status="local_runtime_role_mismatch",
        )
    if mode == "opblock_offline" and role not in _OPERBLOCK_ROLES:
        raise LocalOnlyStartupError(
            "Локальная база оперблока не соответствует выбранной роли.",
            status="local_runtime_role_mismatch",
        )
    for field in _LOCAL_CONTEXT_PATH_FIELDS:
        value = str(getattr(runtime_context, field, "") or "").strip()
        if not value or not os.path.isabs(value) or not _path_is_within(value, local_root):
            raise LocalOnlyStartupError(
                f"Локальный runtime содержит путь вне аварийного каталога: {field}.",
                status="local_runtime_path_escape",
            )


def _default_local_only_confirmation(message: str, open_text: str) -> bool:
    from rem_card.app.main import _call_emergency_startup_offer

    return bool(_call_emergency_startup_offer(message, open_text=open_text))


def _default_local_only_password(settings_db_path: str) -> bool:
    from rem_card.app.main import _show_emergency_startup_password

    return bool(_show_emergency_startup_password(settings_db_path))


def _prepare_local_only_emergency_decision(role: str, local_root: str):
    """Validate only local active/standby files, without constructing network paths."""

    from rem_card.app import emergency_startup as startup
    from rem_card.app.emergency_standby import EmergencyStandbyManager
    from rem_card.app.emergency_store import EmergencyLocalStore

    store = EmergencyLocalStore(root=local_root, source_role=role)
    active_metadata, active_reason = startup.find_resumable_active_session(store)
    if active_metadata is not None:
        return startup.EmergencyStartupDecision(
            role=role,
            allowed=True,
            status="active_session_available",
            user_message=startup.ACTIVE_SESSION_OFFER_MESSAGE,
            root=local_root,
            password_settings_db_path=str(active_metadata.settings_snapshot_path or ""),
            active_session_metadata=active_metadata,
        )
    if active_reason != "no resumable active session":
        startup.record_emergency_startup_metric(
            "emergency_startup_failed",
            status="active_session_invalid",
            reason=active_reason,
        )
        return startup.EmergencyStartupDecision(
            role=role,
            allowed=False,
            status="active_session_invalid",
            user_message=startup.ACTIVE_SESSION_INVALID_MESSAGE,
            root=local_root,
            technical_reason=active_reason,
        )
    if role != "nurse":
        startup.record_emergency_startup_metric("emergency_startup_doctor_blocked", role=role)
        return startup.EmergencyStartupDecision(
            role=role,
            allowed=False,
            status="role_not_allowed",
            user_message=startup.DOCTOR_NETWORK_UNAVAILABLE_MESSAGE,
            root=local_root,
            technical_reason="emergency startup creation is only available for nurse role",
        )

    # EmergencyStandbyManager.__init__ resolves the network runtime even when
    # only validate_standby() is needed.  Build the validator state directly so
    # the selected central root is never imported or inspected in local-only.
    manager = EmergencyStandbyManager.__new__(EmergencyStandbyManager)
    manager.root = local_root
    manager.store = store
    manager.settings_required = True
    standby_status = manager.validate_standby()
    if not standby_status.ok or standby_status.metadata is None:
        reason = standby_status.reason or "no valid standby"
        startup.record_emergency_startup_metric("emergency_startup_no_valid_standby", reason=reason)
        return startup.EmergencyStartupDecision(
            role=role,
            allowed=False,
            status="no_valid_standby",
            user_message=startup._startup_message_with_standby_status(startup.NO_VALID_STANDBY_MESSAGE, standby_status),
            root=local_root,
            technical_reason=reason,
        )
    metadata_ok, metadata_reason = startup._standby_metadata_matches_files(
        standby_status.metadata,
        standby_status,
    )
    if not metadata_ok:
        startup.record_emergency_startup_metric(
            "emergency_startup_no_valid_standby",
            reason=metadata_reason,
        )
        return startup.EmergencyStartupDecision(
            role=role,
            allowed=False,
            status="no_valid_standby",
            user_message=startup._startup_message_with_standby_status(
                startup.NO_VALID_STANDBY_MESSAGE,
                replace(standby_status, ok=False, status="invalid", reason=metadata_reason)),
            root=local_root,
            technical_reason=metadata_reason,
        )
    from rem_card.app.emergency_workflow import validate_emergency_patient_source

    source_error = validate_emergency_patient_source(standby_status.metadata.medical_db_path)
    if source_error:
        return startup.EmergencyStartupDecision(
            role=role,
            allowed=False,
            status="no_valid_standby",
            user_message=startup._startup_message_with_standby_status(
                startup.NO_VALID_STANDBY_MESSAGE,
                replace(standby_status, ok=False, status="invalid", reason=source_error)),
            root=local_root,
            technical_reason=source_error,
        )
    return startup.EmergencyStartupDecision(
        role=role,
        allowed=True,
        status="standby_available",
        user_message=startup._startup_message_with_standby_status(startup.NURSE_EMERGENCY_OFFER_MESSAGE, standby_status),
        root=local_root,
        password_settings_db_path=str(standby_status.metadata.settings_db_path or ""),
        standby_metadata=standby_status.metadata,
    )


def _acquire_local_only_lease(local_root: str, role: str):
    from rem_card.app.unified_access import SessionLease

    lease = SessionLease(local_root, role)
    if not lease.acquire():
        raise LocalOnlyStartupError(
            "Локальный аварийный каталог закрыт для обслуживания.",
            status="local_maintenance_blocked",
        )
    return lease


def prepare_local_only_runtime_context(
    *,
    role: str,
    central_root: str,
    central_failure: str,
    confirm_startup: Callable[[str, str], bool] | None = None,
    confirm_password: Callable[[str], bool] | None = None,
    restart_after_activation: bool = False,
) -> LocalOnlyRuntimeAdmission:
    """Prepare an explicitly confirmed local runtime without probing central.

    Only an unreachable central root is eligible.  A maintenance rejection is
    authoritative and can never enter this path.  ``central_root`` is retained
    as inert identity text; no file operation is performed against it.
    """

    role_key = str(role or "").strip().casefold()
    failure = str(central_failure or "").strip().casefold()
    if failure != CENTRAL_FAILURE_UNREACHABLE:
        raise LocalOnlyStartupError(
            "Локальный запуск разрешён только при недоступности основной базы.",
            status="central_failure_not_unreachable",
        )
    if role_key not in _EMERGENCY_ROLES | _OPERBLOCK_ROLES:
        raise LocalOnlyStartupError(
            "Для выбранной роли локальный аварийный запуск не поддерживается.",
            status="unsupported_local_role",
        )
    central_text = str(central_root or "").strip()
    if not central_text:
        raise LocalOnlyStartupError(
            "Не задан путь основной базы для локального запуска.",
            status="central_root_missing",
        )
    central_identity = _normalized_path(central_text)
    ask = confirm_startup or _default_local_only_confirmation
    password = confirm_password or _default_local_only_password

    if role_key in _EMERGENCY_ROLES:
        from rem_card.app.emergency_paths import resolve_emergency_root
        from rem_card.app.emergency_startup import start_or_resume_emergency_session

        local_root = _require_local_fixed_root(resolve_emergency_root())
        if _same_path(local_root, central_identity):
            raise LocalOnlyStartupError(
                "Аварийный каталог совпадает с основной базой.",
                status="local_root_matches_central",
            )
        decision = _prepare_local_only_emergency_decision(role_key, local_root)
        if not decision.allowed:
            raise LocalOnlyStartupError(
                decision.user_message,
                status=str(decision.status or "emergency_not_available"),
            )
        _require_emergency_source_identity(decision, central_identity)
        open_text = "Войти в программу" if decision.active_session_metadata is not None else "Ввести аварийный пароль"
        if not ask(decision.user_message, open_text):
            raise LocalOnlyStartupCancelled(
                "Пользователь отменил локальный аварийный запуск.",
                status="user_cancelled",
            )
        if decision.active_session_metadata is None and not password(str(decision.password_settings_db_path or "")):
            raise LocalOnlyStartupCancelled(
                "Аварийный пароль не подтверждён.",
                status="password_rejected",
            )
        os.makedirs(local_root, exist_ok=True)
        lease = _acquire_local_only_lease(local_root, role_key)
        try:
            session = start_or_resume_emergency_session(decision, root=local_root)
            runtime_context = session.runtime_context
            _validate_local_runtime_context(role=role_key, runtime_context=runtime_context, local_root=local_root)
            # Validate/authorize the local copy before requesting a clean process.
            # The persisted session survives the restart and never needs a second password.
            require_fresh_local_only_import_state(role=role_key, local_root=local_root)
            if restart_after_activation and decision.active_session_metadata is None:
                raise LocalOnlyRestartRequired(
                    "Аварийная сессия подготовлена. RemCard будет перезапущена.",
                    local_root=local_root, loaded_roots={},
                )
        except BaseException:
            lease.release()
            raise
    else:
        from rem_card.app.operblock_offline_store import (
            OPERBLOCK_OFFLINE_WARNING,
            get_operblock_offline_root,
            start_or_resume_operblock_offline_session,
        )

        local_root = require_fresh_local_only_import_state(
            role=role_key,
            local_root=get_operblock_offline_root(),
        )
        if _same_path(local_root, central_identity):
            raise LocalOnlyStartupError(
                "Локальный каталог оперблока совпадает с основной базой.",
                status="local_root_matches_central",
            )
        if not ask(OPERBLOCK_OFFLINE_WARNING, "Открыть локальный режим"):
            raise LocalOnlyStartupCancelled(
                "Пользователь отменил локальный запуск оперблока.",
                status="user_cancelled",
            )
        os.makedirs(local_root, exist_ok=True)
        lease = _acquire_local_only_lease(local_root, role_key)
        try:
            session = start_or_resume_operblock_offline_session(
                reason="unified_central_unreachable_explicit_local_only",
                network_db_path=None,
                root=local_root,
            )
            runtime_context = session.runtime_context
            _validate_local_runtime_context(role=role_key, runtime_context=runtime_context, local_root=local_root)
        except BaseException:
            lease.release()
            raise

    return LocalOnlyRuntimeAdmission(
        role=role_key,
        central_root=central_identity,
        local_root=local_root,
        runtime_context=runtime_context,
        local_lease=lease,
    )


def local_only_reconnect_advice(admission: LocalOnlyRuntimeAdmission) -> dict[str, Any]:
    """Return the state shell code must enforce before any central retry."""

    return dict(admission.runtime_state)


def configure_local_only_environment(admission: LocalOnlyRuntimeAdmission) -> dict[str, str]:
    """Pin every legacy path fallback to local storage before clinical imports."""

    _validate_local_runtime_context(
        role=admission.role,
        runtime_context=admission.runtime_context,
        local_root=admission.local_root,
    )
    values = {
        LOCAL_ONLY_ENV: "1",
        "REMCARD_UI_ROLE": admission.role,
        "REMCARD_BAZA_DIR": admission.local_root,
        "REMCARD_LOCAL_FIRST_SYNC": "0",
        "REMCARD_LOCAL_OUTBOX_SYNC": "0",
    }
    os.environ.update(values)
    return values


def _set_shell_local_only_state(shell: Any, state: dict[str, Any]) -> None:
    if shell is None:
        return
    setter = getattr(shell, "setProperty", None)
    if callable(setter):
        setter(LOCAL_ONLY_STATE_PROPERTY, dict(state))
    setattr(shell, "_local_only_runtime_state", dict(state))


def _disable_local_only_central_callbacks(container: Any) -> None:
    data_service = getattr(container, "data_service", None)
    schedulers = (
        ("emergency_standby_scheduler", "set_emergency_standby_scheduler"),
        ("emergency_restore_probe_scheduler", "set_emergency_restore_probe_scheduler"),
    )
    for attribute, setter_name in schedulers:
        scheduler = getattr(container, attribute, None)
        if scheduler is not None:
            try:
                stopped = bool(scheduler.stop(timeout=1.0))
            except Exception as exc:
                stopped = False
                stop_error = exc
            else:
                stop_error = None
            if not stopped:
                error = LocalOnlyStartupError(
                    f"Не удалось остановить фоновую проверку основной базы: "
                    f"{stop_error or 'scheduler still running'}",
                    status="central_scheduler_stop_failed",
                )
                error.runtime_container = container
                error.cleanup_failed = True
                raise error
        setattr(container, attribute, None)
        if data_service is not None:
            setter = getattr(data_service, setter_name, None)
            if callable(setter):
                setter(None)
            else:
                setattr(data_service, f"_{attribute}", None)


@contextmanager
def _suppress_central_schedulers_before_bootstrap(bootstrap_func: Callable[..., Any]):
    """Prevent scheduler construction while the local container is built.

    ``EmergencyRestoreProbeScheduler.start`` immediately requests a central
    probe.  Stopping it after ``bootstrap`` therefore has a race.  The unified
    GUI serializes role bootstrap, so a short class-level patch under one lock
    closes that window and is restored before this helper returns.
    """

    module = sys.modules.get(str(getattr(bootstrap_func, "__module__", "") or ""))
    container_type = getattr(module, "Container", None) if module is not None else None
    method_names = (
        "_create_emergency_standby_scheduler",
        "_create_emergency_restore_probe_scheduler",
    )
    originals: dict[str, Any] = {}
    if container_type is None:
        yield
        return
    with _LOCAL_ONLY_BOOTSTRAP_LOCK:
        try:
            for name in method_names:
                method = getattr(container_type, name, None)
                if method is not None:
                    originals[name] = method
                    setattr(container_type, name, lambda self, role: None)
            yield
        finally:
            for name, method in originals.items():
                setattr(container_type, name, method)


def _load_local_bootstrap() -> Callable[..., Any]:
    from rem_card.app.bootstrap import bootstrap

    return bootstrap


def bootstrap_local_only(
    bootstrap: Callable[..., Any] | None = None,
    *,
    admission: LocalOnlyRuntimeAdmission,
    shell: Any = None,
):
    """Bootstrap a validated local context without any central fallback."""

    role_key = str(admission.role or "").strip().casefold()
    lease = admission.local_lease
    if not bool(getattr(lease, "held", False)) or str(getattr(lease, "role", "") or "").casefold() != role_key:
        raise LocalOnlyStartupError(
            "Локальный SessionLease не удерживается выбранной ролью.",
            status="local_lease_not_held",
        )
    lease_root = getattr(getattr(lease, "store", None), "root", "")
    if not _same_path(lease_root, admission.local_root):
        raise LocalOnlyStartupError(
            "Локальный SessionLease относится к другому каталогу.",
            status="local_lease_root_mismatch",
        )
    _validate_local_runtime_context(
        role=role_key,
        runtime_context=admission.runtime_context,
        local_root=admission.local_root,
    )
    require_fresh_local_only_import_state(role=role_key, local_root=admission.local_root)
    state = admission.runtime_state
    configure_local_only_environment(admission)
    _set_shell_local_only_state(shell, state)
    bootstrap_func = bootstrap or _load_local_bootstrap()
    try:
        with _suppress_central_schedulers_before_bootstrap(bootstrap_func):
            container = bootstrap_func(role=role_key, runtime_context=admission.runtime_context)
        _disable_local_only_central_callbacks(container)
    except BaseException:
        # The caller owns admission.local_lease and releases it only after a
        # complete runtime drain, including partial bootstrap owners.
        raise
    container.local_only_runtime_state = dict(state)
    data_service = getattr(container, "data_service", None)
    if data_service is not None:
        data_service.local_only_runtime_state = dict(state)
        data_service.central_access_allowed = False
    return container


__all__ = [
    "CENTRAL_FAILURE_UNREACHABLE",
    "LOCAL_ONLY_ENV",
    "LOCAL_ONLY_STATE_PROPERTY",
    "LocalOnlyRuntimeAdmission",
    "LocalOnlyRestartRequired",
    "LocalOnlyStartupCancelled",
    "LocalOnlyStartupError",
    "UnifiedStartupRequest",
    "attach_startup_request",
    "bootstrap_admitted_container",
    "build_startup_request",
    "complete_startup_request",
    "configure_local_only_environment",
    "get_startup_request",
    "bootstrap_local_only",
    "local_only_reconnect_advice",
    "prepare_admitted_runtime_context",
    "prepare_local_only_runtime_context",
    "require_fresh_local_only_import_state",
    "take_emergency_role_after_chooser_ready",
]
