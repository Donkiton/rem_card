from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from rem_card.app.db_access_classifier import classify_database_access_error
from rem_card.app.emergency_paths import resolve_emergency_root
from rem_card.app.emergency_store import get_or_create_local_emergency_client_id
from rem_card.app.local_metrics import record_metric
from rem_card.app.version import APP_VERSION


RUNTIME_OUTAGE_REASON = "runtime_network_outage"
RUNTIME_OUTAGE_STARTUP_REQUEST_FILE_NAME = "emergency_startup_request.json"
RUNTIME_OUTAGE_STARTUP_REQUEST_TTL_SEC = max(
    60.0,
    float(os.environ.get("REMCARD_RUNTIME_OUTAGE_STARTUP_REQUEST_TTL_SEC", "900")),
)

NURSE_RUNTIME_OUTAGE_MESSAGE = (
    "Сетевая база RemCard стала недоступна.\n\n"
    "Дальнейшая работа должна быть продолжена в аварийном режиме на локальной базе этого ПК.\n\n"
    "Сообщите врачу: до восстановления доступа к сетевой базе работа ведётся только на этом компьютере.\n\n"
    "RemCard будет закрыта и открыта в аварийном режиме."
)

DOCTOR_RUNTIME_OUTAGE_MESSAGE = (
    "Сетевая база недоступна. Работа должна быть продолжена на ПК медсестры."
)

UNCONFIRMED_WRITE_WARNING = (
    "Одна или несколько последних операций не были подтверждены сетевой базой.\n"
    "Проверьте их после открытия аварийного режима."
)

UNKNOWN_WRITE_WARNING = "Последняя операция не подтверждена сетевой базой."

STALE_STANDBY_WARNING = (
    "Аварийная копия может не содержать последние неподтверждённые изменения.\n"
    "Проверьте последние записи после открытия."
)


class RuntimeNetworkOutageWriteBlockedError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeOutageStartupRequestValidation:
    ok: bool
    reason: str
    payload: dict[str, Any]


def runtime_outage_startup_request_path(root: str | None = None) -> str:
    return os.path.join(resolve_emergency_root(root), RUNTIME_OUTAGE_STARTUP_REQUEST_FILE_NAME)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _requested_at_epoch(payload: dict[str, Any]) -> float:
    raw_epoch = payload.get("requested_at_epoch")
    if isinstance(raw_epoch, (int, float)):
        return float(raw_epoch)
    raw_text = str(payload.get("requested_at") or "").strip()
    if not raw_text:
        return 0.0
    try:
        return datetime.fromisoformat(raw_text).timestamp()
    except Exception:
        return 0.0


def runtime_outage_transition_allowed(category: str, *, repeated: bool = False) -> bool:
    normalized = str(category or "").strip().lower()
    if normalized == "network_unavailable":
        return True
    if normalized == "locked_busy" and repeated:
        return True
    return False


def build_runtime_outage_dialog_message(
    *,
    unconfirmed_writes: bool = False,
    unknown_active_write: bool = False,
    stale_standby: bool = False,
) -> str:
    parts = [NURSE_RUNTIME_OUTAGE_MESSAGE]
    if unconfirmed_writes:
        parts.append(UNCONFIRMED_WRITE_WARNING)
    elif unknown_active_write:
        parts.append(UNKNOWN_WRITE_WARNING)
    if stale_standby:
        parts.append(STALE_STANDBY_WARNING)
    return "\n\n".join(parts)


def build_doctor_runtime_outage_message() -> str:
    return DOCTOR_RUNTIME_OUTAGE_MESSAGE


def build_runtime_outage_startup_request_payload(
    *,
    source_role: str,
    reason: str = RUNTIME_OUTAGE_REASON,
    source_client_id: str | None = None,
    source_pid: int | None = None,
    last_observed_remote_change_id: int | None = None,
    standby_last_change_id: int | None = None,
    outage_detected_at: str | None = None,
    unconfirmed_writes: bool = False,
    pending_write_count: int = 0,
    unconfirmed_write_count: int = 0,
    unknown_active_write: bool = False,
    queue_shutdown_result: str = "",
    queue_settled: bool | None = None,
) -> dict[str, Any]:
    observed = int(last_observed_remote_change_id or 0)
    standby = int(standby_last_change_id or 0)
    stale_gap = observed > standby if standby >= 0 else False
    requested_at = _now_iso()
    return {
        "requested_at": requested_at,
        "created_at": requested_at,
        "requested_at_epoch": time.time(),
        "ttl_sec": float(RUNTIME_OUTAGE_STARTUP_REQUEST_TTL_SEC),
        "source_role": str(source_role or ""),
        "reason": str(reason or RUNTIME_OUTAGE_REASON),
        "source_pid": int(source_pid or os.getpid()),
        "source_client_id": str(source_client_id or ""),
        "app_version": APP_VERSION,
        "metadata_schema_version": 1,
        "stale_gap_detected": bool(stale_gap),
        "last_observed_remote_change_id": observed,
        "standby_last_change_id": standby,
        "outage_detected_at": str(outage_detected_at or requested_at),
        "pending_write_count": max(0, int(pending_write_count or 0)),
        "unconfirmed_write_count": max(0, int(unconfirmed_write_count or 0)),
        "unknown_active_write": bool(unknown_active_write),
        "queue_shutdown_result": str(queue_shutdown_result or ""),
        "queue_settled": queue_settled,
        "unconfirmed_writes": bool(
            unconfirmed_writes
            or int(unconfirmed_write_count or 0) > 0
            or bool(unknown_active_write)
            or str(queue_shutdown_result or "") in {"timeout", "failed"}
        ),
    }


def write_runtime_outage_startup_request(
    *,
    root: str | None = None,
    source_role: str = "nurse",
    last_observed_remote_change_id: int | None = None,
    standby_last_change_id: int | None = None,
    unconfirmed_writes: bool = False,
    pending_write_count: int = 0,
    unconfirmed_write_count: int = 0,
    unknown_active_write: bool = False,
    queue_shutdown_result: str = "",
    queue_settled: bool | None = None,
) -> tuple[str, dict[str, Any]]:
    resolved_root = resolve_emergency_root(root)
    os.makedirs(resolved_root, exist_ok=True)
    payload = build_runtime_outage_startup_request_payload(
        source_role=source_role,
        source_client_id=get_or_create_local_emergency_client_id(resolved_root),
        last_observed_remote_change_id=last_observed_remote_change_id,
        standby_last_change_id=standby_last_change_id,
        unconfirmed_writes=unconfirmed_writes,
        pending_write_count=pending_write_count,
        unconfirmed_write_count=unconfirmed_write_count,
        unknown_active_write=unknown_active_write,
        queue_shutdown_result=queue_shutdown_result,
        queue_settled=queue_settled,
    )
    marker_path = runtime_outage_startup_request_path(resolved_root)
    tmp_path = f"{marker_path}.{os.getpid()}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp_path, marker_path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    record_metric(
        "runtime_outage_emergency_startup_request_written",
        1,
        marker_path=marker_path,
        stale_gap_detected=payload.get("stale_gap_detected"),
    )
    return marker_path, payload


def validate_runtime_outage_startup_request_marker(
    marker_path: str | None,
    *,
    now_epoch: float | None = None,
    ttl_sec: float = RUNTIME_OUTAGE_STARTUP_REQUEST_TTL_SEC,
) -> RuntimeOutageStartupRequestValidation:
    path = os.path.abspath(os.path.normpath(str(marker_path or "")))
    if not path or not os.path.isfile(path):
        return RuntimeOutageStartupRequestValidation(False, "startup request marker is missing", {})
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception as exc:
        return RuntimeOutageStartupRequestValidation(False, f"startup request marker is invalid: {exc}", {})
    if not isinstance(payload, dict):
        return RuntimeOutageStartupRequestValidation(False, "startup request marker payload is not an object", {})
    if str(payload.get("reason") or "") != RUNTIME_OUTAGE_REASON:
        return RuntimeOutageStartupRequestValidation(False, "startup request marker reason mismatch", payload)
    if str(payload.get("source_role") or "") != "nurse":
        return RuntimeOutageStartupRequestValidation(False, "startup request marker role mismatch", payload)
    requested_at = _requested_at_epoch(payload)
    now = float(now_epoch if now_epoch is not None else time.time())
    if requested_at <= 0 or (now - requested_at) > max(1.0, float(ttl_sec or 0.0)):
        return RuntimeOutageStartupRequestValidation(False, "startup request marker expired", payload)
    return RuntimeOutageStartupRequestValidation(True, "ok", payload)


def startup_request_stale_warning(payload: dict[str, Any] | None) -> str:
    if payload and bool(payload.get("stale_gap_detected")):
        return STALE_STANDBY_WARNING
    return ""


def _runtime_restart_args(marker_path: str, *, role: str = "nurse") -> list[str]:
    marker_abs = os.path.abspath(marker_path)
    normalized_role = str(role or "nurse")
    if getattr(sys, "frozen", False):
        return [
            os.path.abspath(sys.executable),
            "--role",
            normalized_role,
            "--emergency-startup-request",
            marker_abs,
        ]

    argv0 = str(sys.argv[0] if sys.argv else "").strip()
    script_path = os.path.abspath(argv0) if argv0 and os.path.isfile(argv0) else ""
    if not script_path:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        role_script = {
            "doctor": "run_doctor.py",
            "nurse": "run_nurse.py",
            "operblock": "run_operblock.py",
        }.get(normalized_role, "launcher.py")
        script_path = os.path.join(project_root, role_script)
    return [
        os.path.abspath(sys.executable),
        script_path,
        "--role",
        normalized_role,
        "--emergency-startup-request",
        marker_abs,
    ]


def launch_emergency_restart(
    marker_path: str,
    *,
    role: str = "nurse",
    launcher: Callable[[list[str]], Any] | None = None,
) -> bool:
    args = _runtime_restart_args(marker_path, role=role)
    if launcher is not None:
        launcher(args)
        return True

    parent_pid = os.getpid()
    ps_command = (
        "$ErrorActionPreference='SilentlyContinue'; "
        f"$p=Get-Process -Id {parent_pid}; "
        f"if ($p) {{ Wait-Process -Id {parent_pid} -Timeout 30 }}; "
        "Start-Process -FilePath $args[0] -ArgumentList $args[1..($args.Length-1)]"
    )
    command = [
        "powershell",
        "-NoProfile",
        "-WindowStyle",
        "Hidden",
        "-Command",
        ps_command,
        *args,
    ]
    try:
        cwd = os.path.dirname(args[0]) or None
        if len(args) > 1 and os.path.isfile(args[1]):
            cwd = os.path.dirname(args[1]) or cwd
        subprocess.Popen(command, cwd=cwd)
    except Exception as exc:
        record_metric("runtime_outage_emergency_relaunch_failed", 1, error=str(exc))
        return False
    record_metric("runtime_outage_emergency_relaunch_requested", 1, marker_path=marker_path)
    return True


def should_request_runtime_outage_transition(exc: object, *, repeated_locked_busy: bool = False) -> bool:
    category = classify_database_access_error(exc)
    return runtime_outage_transition_allowed(category, repeated=repeated_locked_busy)
