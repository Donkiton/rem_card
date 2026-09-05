#!/usr/bin/env python
r"""
Regression checks for SQLite safety, local replica hygiene and backup cleanup gating.

Usage:
  python %REMCARD_PROJECT_ROOT%\scripts\regression_safety_checks.py
"""

from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import faulthandler
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Any


DEFAULT_REGRESSION_TIMEOUT_SEC = 600.0
DIRECT_REGRESSION_TEMP_PREFIX = "remcard_regression_checks_direct_"
WORKER_REGRESSION_TEMP_PREFIX = "remcard_regression_checks_w"
try:
    DEFAULT_FAST_JOBS = max(1, min(4, int(os.environ.get("REMCARD_REGRESSION_JOBS", "4"))))
except (TypeError, ValueError):
    DEFAULT_FAST_JOBS = 4





if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from _local_rem_card_bootstrap import bootstrap_local_rem_card

    bootstrap_local_rem_card()
except Exception:
    pass

from scripts.regression_checks.common import (  # noqa: E402
    PROJECT_ROOT,
    _REGRESSION_RESTORE_PROBES,
    _cached_source_segment as _cached_source_segment,
)
from scripts.regression_checks.paths import (  # noqa: E402
    _check_arbitrary_baza_dir_name_allowed as _check_arbitrary_baza_dir_name_allowed,
)
from scripts.regression_checks.registry import get_checks  # noqa: E402
from scripts.regression_checks.ui_layout import (  # noqa: E402
    _check_w1_days_label_scope_by_bed_type_runtime as _check_w1_days_label_scope_by_bed_type_runtime,
)
from scripts.regression_checks.scheduling import (  # noqa: E402
    partition_checks,
    shard_execution_order,
    timing_estimates,
)

# Совместимость диагностических скриптов и инфраструктурных тестов.
__all__ = [
    "_cached_source_segment", "_check_arbitrary_baza_dir_name_allowed",
    "_check_w1_days_label_scope_by_bed_type_runtime", "get_checks", "main",
]


def _make_temp_root() -> str:
    explicit_root = os.environ.get("REMCARD_REGRESSION_TEMP_ROOT")
    if explicit_root:
        root = Path(explicit_root).resolve()
        system_temp = Path(tempfile.gettempdir()).resolve()
        if (
            not root.name.startswith("remcard_regression_checks_")
            or os.path.commonpath([str(root), str(system_temp)]) != str(system_temp)
        ):
            raise RuntimeError(f"Unsafe REMCARD_REGRESSION_TEMP_ROOT name: {root}")
        root.mkdir(parents=True, exist_ok=True)
        return str(root)
    return tempfile.mkdtemp(prefix=f"{DIRECT_REGRESSION_TEMP_PREFIX}{os.getpid()}_")


def _safe_regression_temp_path(path: Path) -> bool:
    try:
        resolved = path.resolve(strict=False)
        system_temp = Path(tempfile.gettempdir()).resolve(strict=False)
        return (
            resolved.name.startswith((DIRECT_REGRESSION_TEMP_PREFIX, WORKER_REGRESSION_TEMP_PREFIX))
            and os.path.commonpath([str(resolved), str(system_temp)]) == str(system_temp)
        )
    except Exception:
        return False


def _rmtree_regression_root(path: str | Path) -> str:
    target = Path(path)
    if not target.exists():
        return ""
    try:
        resolved = target.resolve(strict=False)
        system_temp = Path(tempfile.gettempdir()).resolve(strict=False)
        safe = (
            resolved.name.startswith("remcard_regression_checks_")
            and os.path.commonpath([str(resolved), str(system_temp)]) == str(system_temp)
        )
    except Exception as exc:
        return f"failed to validate temp cleanup path {target}: {exc}"
    if not safe:
        return f"refused unsafe temp cleanup path: {resolved}"

    delete_path = str(resolved)
    if os.name == "nt" and not delete_path.startswith("\\\\?\\"):
        delete_path = "\\\\?\\" + delete_path

    def make_writable_and_retry(function, item_path, _exc_info):
        os.chmod(item_path, stat.S_IRWXU)
        function(item_path)

    last_error = ""
    for attempt in range(3):
        try:
            shutil.rmtree(delete_path, onerror=make_writable_and_retry)
            return ""
        except FileNotFoundError:
            return ""
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < 2:
                time.sleep(0.1)
    return f"failed to remove regression temp root {resolved}: {last_error}"


def _owner_pid_from_direct_temp_name(name: str) -> int | None:
    if name.startswith(DIRECT_REGRESSION_TEMP_PREFIX):
        tail = name[len(DIRECT_REGRESSION_TEMP_PREFIX):]
    elif name.startswith(WORKER_REGRESSION_TEMP_PREFIX):
        tail = name[len(WORKER_REGRESSION_TEMP_PREFIX):]
    else:
        return None
    pid_text = tail.split("_", 1)[0]
    try:
        return int(pid_text)
    except (TypeError, ValueError):
        return None


def _pid_is_running(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
            return str(pid) in (result.stdout or "")
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _cleanup_orphan_direct_temp_roots() -> None:
    system_temp = Path(tempfile.gettempdir())
    candidates = list(system_temp.glob(f"{DIRECT_REGRESSION_TEMP_PREFIX}*"))
    candidates.extend(system_temp.glob(f"{WORKER_REGRESSION_TEMP_PREFIX}*"))
    for path in candidates:
        if not path.is_dir() or not _safe_regression_temp_path(path):
            continue
        owner_pid = _owner_pid_from_direct_temp_name(path.name)
        if _pid_is_running(owner_pid):
            continue
        _rmtree_regression_root(path)


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Regression checks for RemCard safety contracts")
    parser.add_argument(
        "--profile",
        choices=("fast", "exhaustive"),
        default=os.environ.get("REMCARD_REGRESSION_PROFILE", "fast"),
        help=(
            "fast runs the complete check registry in isolated parallel shards; "
            "exhaustive runs the same registry sequentially in the historical order."
        ),
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=0,
        help="Worker count for the fast profile (0 uses REMCARD_REGRESSION_JOBS or up to 4).",
    )
    parser.add_argument(
        "--shards",
        type=int,
        default=0,
        help="Duration-balanced contiguous shards (0 uses four shards per worker).",
    )
    parser.add_argument("--worker-shard-index", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--worker-shard-count", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=_float_env("REMCARD_REGRESSION_TIMEOUT_SEC", DEFAULT_REGRESSION_TIMEOUT_SEC),
        help=(
            "Hard timeout for the whole regression process. "
            "Set 0 to disable. Can also be set with REMCARD_REGRESSION_TIMEOUT_SEC."
        ),
    )
    parser.add_argument(
        "--quiet-progress",
        action="store_true",
        help="Do not print per-check progress lines before the final JSON report.",
    )
    parser.add_argument(
        "--json-detail",
        choices=("summary", "all"),
        default="summary",
        help="summary prints failures and slowest checks; all prints every check result.",
    )
    parser.add_argument(
        "--report-path",
        default="",
        help="Optional path for the complete JSON report, independent of stdout detail.",
    )
    return parser.parse_args(argv)


def _print_progress(message: str, *, quiet: bool) -> None:
    if not quiet:
        print(message, flush=True)


def _configure_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except (OSError, ValueError):
                pass


def _emit_regression_report(
    report: dict[str, Any],
    *,
    json_detail: str,
    report_path: str = "",
) -> None:
    resolved_report_path = ""
    if report_path:
        target = Path(report_path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        resolved_report_path = str(target)
    if json_detail == "all":
        output = report
    else:
        checks = list(report.get("checks") or [])
        output = {key: value for key, value in report.items() if key != "checks"}
        output["checks"] = [item for item in checks if not item.get("ok")]
        output["slowest_checks"] = [
            {
                "check": item.get("check"),
                "duration_sec": item.get("duration_sec", 0.0),
            }
            for item in sorted(
                (item for item in checks if item.get("check") not in {"__timeout__"}),
                key=lambda item: float(item.get("duration_sec", 0.0) or 0.0),
                reverse=True,
            )[:15]
        ]
        names = [str(item.get("check")) for item in checks if not str(item.get("check")).startswith("__")]
        output["check_manifest_sha256"] = hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()
        output["detailed_report_path"] = resolved_report_path
    print(json.dumps(output, ensure_ascii=False, indent=2))


def _prepare_import_environment(temp_root: str):
    # Isolate LOCALAPPDATA so tests do not touch real user cache.
    os.environ["LOCALAPPDATA"] = os.path.join(temp_root, "localappdata")
    os.environ["REMCARD_BAZA_DIR"] = os.path.join(temp_root, "regression_data_root")
    os.environ["REMCARD_LOCAL_LOGS_DIR"] = os.path.join(temp_root, "logs")
    os.environ["REMCARD_LOCAL_FIRST_SYNC"] = "0"
    os.environ["REMCARD_LOCAL_SYNC_INTERVAL_SEC"] = "999"
    os.environ["REMCARD_LOCAL_OUTBOX_SYNC"] = "0"
    os.environ["REMCARD_LOCAL_CACHE_RETENTION_DAYS"] = "3"
    os.environ["REMCARD_LOCAL_CACHE_MAX_FILES"] = "200"


def _cleanup_check_resources() -> list[str]:
    """Release runner-owned background resources before the next check."""
    errors: list[str] = []
    while _REGRESSION_RESTORE_PROBES:
        probe = _REGRESSION_RESTORE_PROBES.pop()
        try:
            probe.release_network_emergency_role_marker()
        except Exception as exc:
            errors.append(f"restore probe cleanup: {type(exc).__name__}: {exc}")
    leaked_heartbeats = [
        thread.name
        for thread in threading.enumerate()
        if thread.is_alive() and thread.name.startswith("RoleLockHeartbeat:")
    ]
    if leaked_heartbeats:
        errors.append(f"leaked role lock heartbeat threads: {sorted(leaked_heartbeats)}")
    return errors


def _select_worker_shard(
    checks: list[tuple[str, Any]],
    *,
    shard_index: int,
    shard_count: int,
) -> list[tuple[str, Any]]:
    if shard_count < 1 or not 0 <= shard_index < shard_count:
        raise ValueError("invalid worker shard selection")
    return partition_checks(checks, shard_count, timing_estimates())[shard_index]


def _extract_worker_report(output: str) -> dict[str, Any] | None:
    marker = '{\n  "total"'
    start = output.rfind(marker)
    if start < 0:
        return None
    try:
        payload = json.loads(output[start:])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _terminate_worker_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _looks_like_native_worker_crash(return_code: int | None, stderr: str = "") -> bool:
    if return_code is None:
        return False
    if int(return_code) < 0:
        return True
    unsigned_code = int(return_code) & 0xFFFFFFFF
    if unsigned_code in {
        0xC0000005,  # access violation
        0xC000001D,  # illegal instruction
        0xC0000374,  # heap corruption
        0xC0000409,  # stack buffer overrun / fail-fast
    }:
        return True
    lowered = str(stderr or "").lower()
    return any(
        marker in lowered
        for marker in (
            "fatal python error: access violation",
            "segmentation fault",
            "stack buffer overrun",
            "heap corruption",
        )
    )


def _run_isolated_worker_command(
    *,
    command: list[str],
    shard_index: int,
    deadline_monotonic: float | None,
    env: dict[str, str],
    cleanup_root: str | Path | None = None,
) -> dict[str, Any]:
    """Run one shard process inside the shared fast-profile wall-clock budget."""
    started = time.monotonic()
    if deadline_monotonic is not None and started >= deadline_monotonic:
        cleanup_error = _rmtree_regression_root(cleanup_root) if cleanup_root else ""
        error = "global regression timeout reached before worker start"
        if cleanup_error:
            error = f"{error}; {cleanup_error}"
        return {
            "shard_index": shard_index,
            "exit_code": None,
            "duration_sec": 0.0,
            "error": error,
            "failure_kind": "timeout",
            "timed_out": True,
            "crashed": False,
            "native_crash": False,
            "stdout_tail": "",
            "stderr_tail": "",
            "payload": None,
        }

    proc = subprocess.Popen(
        command,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    timed_out = False
    try:
        remaining = (
            None
            if deadline_monotonic is None
            else max(0.0, deadline_monotonic - time.monotonic())
        )
        stdout, stderr = proc.communicate(timeout=remaining)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_worker_process(proc)
        stdout, stderr = proc.communicate()

    payload = None if timed_out else (_extract_worker_report(stdout) or _extract_worker_report(stderr))
    return_code = proc.returncode
    crashed = not timed_out and return_code not in (0, 1)
    native_crash = crashed and _looks_like_native_worker_crash(return_code, stderr)
    error = ""
    failure_kind = ""
    if timed_out:
        failure_kind = "timeout"
        error = (
            f"worker exceeded global regression timeout; exit={return_code}; "
            f"stdout_tail={stdout[-800:]!r}; stderr_tail={stderr[-800:]!r}"
        )
    elif crashed:
        failure_kind = "native_crash" if native_crash else "abnormal_exit"
        error = (
            f"worker {failure_kind}={return_code}; "
            f"stdout_tail={stdout[-800:]!r}; stderr_tail={stderr[-800:]!r}"
        )
    elif payload is None:
        failure_kind = "invalid_report"
        error = (
            f"worker exit={return_code} without valid JSON report; "
            f"stdout_tail={stdout[-800:]!r}; stderr_tail={stderr[-800:]!r}"
        )
    elif return_code == 1:
        failure_kind = "test_failure"

    cleanup_error = _rmtree_regression_root(cleanup_root) if cleanup_root else ""
    if cleanup_error:
        error = "; ".join(item for item in (error, cleanup_error) if item)
        if not failure_kind or failure_kind == "test_failure":
            failure_kind = "cleanup"

    return {
        "shard_index": shard_index,
        "exit_code": return_code,
        "duration_sec": round(time.monotonic() - started, 3),
        "error": error,
        "failure_kind": failure_kind,
        "timed_out": timed_out,
        "crashed": crashed,
        "native_crash": native_crash,
        "stdout_tail": stdout[-1600:],
        "stderr_tail": stderr[-1600:],
        "payload": payload,
    }


def _run_worker_process(
    *,
    shard_index: int,
    shard_count: int,
    deadline_monotonic: float | None,
    temp_root: str,
) -> dict[str, Any]:
    _ = temp_root
    remaining = (
        0.0
        if deadline_monotonic is not None and deadline_monotonic <= time.monotonic()
        else (
            max(0.001, deadline_monotonic - time.monotonic())
            if deadline_monotonic is not None
            else 0.0
        )
    )
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--profile",
        "exhaustive",
        "--jobs",
        "1",
        "--worker-shard-index",
        str(shard_index),
        "--worker-shard-count",
        str(shard_count),
        "--timeout-s",
        str(remaining),
        "--quiet-progress",
        "--json-detail",
        "all",
    ]
    env = os.environ.copy()
    short_worker_root = Path(tempfile.gettempdir()) / (
        f"remcard_regression_checks_w{os.getpid()}_{shard_index}"
    )
    env["REMCARD_REGRESSION_TEMP_ROOT"] = str(short_worker_root)
    return _run_isolated_worker_command(
        command=command,
        shard_index=shard_index,
        deadline_monotonic=deadline_monotonic,
        env=env,
        cleanup_root=short_worker_root,
    )


def _worker_report_is_structurally_valid(worker: dict[str, Any]) -> bool:
    payload = worker.get("payload")
    checks = payload.get("checks") if isinstance(payload, dict) else None
    return (
        worker.get("exit_code") in (0, 1)
        and isinstance(payload, dict)
        and isinstance(checks, list)
        and all(isinstance(item, dict) for item in checks)
        and not worker.get("error")
        and not worker.get("timed_out")
        and not worker.get("crashed")
    )


def _parallel_wait_timeout(deadline_monotonic: float | None) -> float:
    if deadline_monotonic is None:
        return 15.0
    remaining = deadline_monotonic - time.monotonic()
    return min(15.0, max(0.1, remaining))


def _orchestrator_error_worker(
    *,
    shard_index: int,
    started_monotonic: float,
    error: Exception,
) -> dict[str, Any]:
    return {
        "shard_index": shard_index,
        "exit_code": None,
        "duration_sec": round(time.monotonic() - started_monotonic, 3),
        "error": f"{type(error).__name__}: {error}",
        "failure_kind": "orchestrator_error",
        "timed_out": False,
        "crashed": False,
        "native_crash": False,
        "payload": None,
        "stdout_tail": "",
        "stderr_tail": "",
    }


def _append_parallel_worker_result(
    worker: dict[str, Any],
    *,
    results: list[dict[str, Any]],
    worker_meta: list[dict[str, Any]],
) -> None:
    worker_meta.append(worker)
    payload = worker.get("payload")
    if not isinstance(payload, dict):
        return
    worker_checks = payload.get("checks")
    if isinstance(worker_checks, list):
        results.extend(item for item in worker_checks if isinstance(item, dict))


def _parallel_results_from_workers(worker_meta: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for worker in worker_meta:
        payload = worker.get("payload")
        worker_checks = payload.get("checks") if isinstance(payload, dict) else None
        if isinstance(worker_checks, list):
            results.extend(item for item in worker_checks if isinstance(item, dict))
    return results


def _retry_native_crash_workers(
    worker_meta: list[dict[str, Any]],
    *,
    shard_count: int,
    deadline_monotonic: float | None,
    temp_root: str,
    quiet: bool,
) -> int:
    retry_count = 0
    for position, worker in enumerate(worker_meta):
        if not worker.get("native_crash"):
            continue
        shard_index = int(worker["shard_index"])
        _print_progress(
            f"[regression] shard {shard_index + 1}/{shard_count} native crash; retrying once in isolation",
            quiet=quiet,
        )
        retry = _run_worker_process(
            shard_index=shard_index,
            shard_count=shard_count,
            deadline_monotonic=deadline_monotonic,
            temp_root=temp_root,
        )
        retry["retried_native_crash"] = True
        retry["initial_exit_code"] = worker.get("exit_code")
        retry["initial_error"] = worker.get("error", "")
        worker_meta[position] = retry
        retry_count += 1
    return retry_count


def _await_parallel_workers(
    futures: dict[Any, int],
    *,
    started_monotonic: float,
    deadline_monotonic: float | None,
    shard_count: int,
    quiet: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    results: list[dict[str, Any]] = []
    worker_meta: list[dict[str, Any]] = []
    pending = set(futures)
    while pending:
        done, pending = wait(
            pending,
            timeout=_parallel_wait_timeout(deadline_monotonic),
            return_when=FIRST_COMPLETED,
        )
        if not done:
            elapsed = time.monotonic() - started_monotonic
            _print_progress(
                f"[regression] shards running={len(pending)}/{shard_count} elapsed={elapsed:.1f}s",
                quiet=quiet,
            )
            continue
        for future in done:
            index = futures[future]
            try:
                worker = future.result()
            except Exception as exc:
                worker = _orchestrator_error_worker(
                    shard_index=index,
                    started_monotonic=started_monotonic,
                    error=exc,
                )
            _append_parallel_worker_result(worker, results=results, worker_meta=worker_meta)
            _print_progress(
                f"[regression] shard {index + 1}/{shard_count} done "
                f"exit={worker.get('exit_code')} duration={worker.get('duration_sec')}s",
                quiet=quiet,
            )
    return results, worker_meta


def _parallel_coverage(
    expected_names: list[str],
    results: list[dict[str, Any]],
    worker_meta: list[dict[str, Any]],
) -> tuple[list[str], list[str], list[str], list[dict[str, Any]]]:
    seen_names = [str(item.get("check")) for item in results if item.get("check") != "__timeout__"]
    missing = sorted(set(expected_names) - set(seen_names))
    duplicates = sorted(name for name in set(seen_names) if seen_names.count(name) > 1)
    unexpected = sorted(set(seen_names) - set(expected_names))
    infrastructure_failures = [
        {
            "check": f"__worker_{int(worker['shard_index'])}__",
            "ok": False,
            "details": worker.get("error") or "worker returned no valid JSON report",
            "duration_sec": worker.get("duration_sec", 0.0),
        }
        for worker in sorted(worker_meta, key=lambda item: int(item["shard_index"]))
        if not _worker_report_is_structurally_valid(worker)
    ]
    if missing or duplicates or unexpected:
        infrastructure_failures.append(
            {
                "check": "__coverage__",
                "ok": False,
                "details": f"missing={missing}; duplicates={duplicates}; unexpected={unexpected}",
                "duration_sec": 0.0,
            }
        )
    return missing, duplicates, unexpected, infrastructure_failures


def _order_parallel_results(
    expected_names: list[str],
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return sorted(
        results,
        key=lambda item: (
            expected_names.index(str(item.get("check")))
            if str(item.get("check")) in expected_names
            else len(expected_names),
            str(item.get("check")),
        ),
    )


def _parallel_worker_summary(worker: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "shard_index": worker["shard_index"],
        "exit_code": worker.get("exit_code"),
        "duration_sec": worker.get("duration_sec"),
        "error": worker.get("error", ""),
        "failure_kind": worker.get("failure_kind", ""),
        "timed_out": bool(worker.get("timed_out")),
        "crashed": bool(worker.get("crashed")),
        "native_crash": bool(worker.get("native_crash")),
    }
    if worker.get("retried_native_crash"):
        summary["retried_native_crash"] = True
        summary["initial_exit_code"] = worker.get("initial_exit_code")
        summary["initial_error"] = worker.get("initial_error", "")
    return summary


def _build_parallel_report(
    *,
    checks: list[tuple[str, Any]],
    expected_names: list[str],
    ordered_results: list[dict[str, Any]],
    worker_meta: list[dict[str, Any]],
    infrastructure_failures: list[dict[str, Any]],
    missing: list[str],
    duplicates: list[str],
    unexpected: list[str],
    jobs: int,
    shard_count: int,
    started_monotonic: float,
) -> tuple[dict[str, Any], int]:
    failures = sum(1 for item in ordered_results if not item.get("ok"))
    report = {
        "total": len(checks),
        "failed": failures,
        "passed": sum(1 for item in ordered_results if item.get("ok")),
        "duration_sec": round(time.monotonic() - started_monotonic, 3),
        "timed_out": (
            any(item.get("check") == "__timeout__" for item in ordered_results)
            or any(bool(worker.get("timed_out")) for worker in worker_meta)
        ),
        "worker_crash": any(bool(worker.get("crashed")) for worker in worker_meta),
        "native_crash": any(bool(worker.get("native_crash")) for worker in worker_meta),
        "native_crash_retries": sum(
            1 for worker in worker_meta if worker.get("retried_native_crash")
        ),
        "native_crash_recovered": any(
            worker.get("retried_native_crash") and _worker_report_is_structurally_valid(worker)
            for worker in worker_meta
        ),
        "completed": sum(1 for item in ordered_results if str(item.get("check")) in expected_names),
        "profile": "fast",
        "jobs": jobs,
        "shards": shard_count,
        "coverage_complete": not missing and not duplicates and not unexpected and not infrastructure_failures,
        "workers": [
            _parallel_worker_summary(worker)
            for worker in sorted(worker_meta, key=lambda item: int(item["shard_index"]))
        ],
        "checks": ordered_results,
    }
    return report, failures


def _run_parallel_profile(
    checks: list[tuple[str, Any]],
    *,
    jobs: int,
    shard_count: int,
    timeout_s: float,
    temp_root: str,
    quiet: bool,
    json_detail: str,
    report_path: str,
    deadline_monotonic: float | None = None,
) -> None:
    started = time.monotonic()
    if deadline_monotonic is None and timeout_s > 0:
        deadline_monotonic = started + timeout_s
    expected_names = [name for name, _fn in checks]
    shard_count = max(jobs, min(shard_count, len(checks)))
    _print_progress(
        f"[regression] fast profile start total={len(checks)} jobs={jobs} shards={shard_count} timeout_s={timeout_s:g}",
        quiet=quiet,
    )
    with ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="RegressionShard") as executor:
        futures = {
            executor.submit(
                _run_worker_process,
                shard_index=index,
                shard_count=shard_count,
                deadline_monotonic=deadline_monotonic,
                temp_root=temp_root,
            ): index
            # Сначала запускаем самые тяжёлые группы по сохранённым измерениям.
            # Порядок проверок внутри каждой группы остаётся историческим.
            for index in shard_execution_order(checks, shard_count)
        }
        results, worker_meta = _await_parallel_workers(
            futures,
            started_monotonic=started,
            deadline_monotonic=deadline_monotonic,
            shard_count=shard_count,
            quiet=quiet,
        )

    _retry_native_crash_workers(
        worker_meta,
        shard_count=shard_count,
        deadline_monotonic=deadline_monotonic,
        temp_root=temp_root,
        quiet=quiet,
    )
    results = _parallel_results_from_workers(worker_meta)

    missing, duplicates, unexpected, infrastructure_failures = _parallel_coverage(
        expected_names,
        results,
        worker_meta,
    )
    results.extend(infrastructure_failures)
    ordered_results = _order_parallel_results(expected_names, results)
    report, failures = _build_parallel_report(
        checks=checks,
        expected_names=expected_names,
        ordered_results=ordered_results,
        worker_meta=worker_meta,
        infrastructure_failures=infrastructure_failures,
        missing=missing,
        duplicates=duplicates,
        unexpected=unexpected,
        jobs=jobs,
        shard_count=shard_count,
        started_monotonic=started,
    )
    _emit_regression_report(report, json_detail=json_detail, report_path=report_path)
    raise SystemExit(1 if failures else 0)


def main(argv: list[str] | None = None):
    _configure_utf8_console()
    args = _parse_args(argv)
    timeout_s = max(0.0, float(args.timeout_s or 0.0))
    deadline_monotonic = time.monotonic() + timeout_s if timeout_s > 0 else None
    if timeout_s > 0:
        faulthandler.enable()
        faulthandler.dump_traceback_later(timeout_s, repeat=False, exit=False)

    _cleanup_orphan_direct_temp_roots()
    temp_root = _make_temp_root()
    _prepare_import_environment(temp_root)

    checks = get_checks()

    shard_index = args.worker_shard_index
    shard_count = args.worker_shard_count
    if (shard_index is None) != (shard_count is None):
        raise SystemExit("worker shard index and count must be provided together")
    if shard_index is not None:
        if shard_count is None or shard_count < 1 or not 0 <= shard_index < shard_count:
            raise SystemExit("invalid worker shard selection")
        checks = _select_worker_shard(
            checks,
            shard_index=shard_index,
            shard_count=shard_count,
        )
    elif args.profile == "fast":
        jobs = int(args.jobs or DEFAULT_FAST_JOBS)
        jobs = max(1, min(jobs, len(checks)))
        shard_count = int(args.shards or jobs * 4)
        shard_count = max(jobs, min(shard_count, len(checks)))
        try:
            _run_parallel_profile(
                checks,
                jobs=jobs,
                shard_count=shard_count,
                timeout_s=timeout_s,
                temp_root=temp_root,
                quiet=bool(args.quiet_progress),
                json_detail=str(args.json_detail),
                report_path=str(args.report_path or ""),
                deadline_monotonic=deadline_monotonic,
            )
        finally:
            _rmtree_regression_root(temp_root)
            if timeout_s > 0:
                faulthandler.cancel_dump_traceback_later()

    result_items = []
    failures = 0
    started = time.time()
    try:
        total = len(checks)
        _print_progress(
            f"[regression] start total={total} timeout_s={timeout_s:g}",
            quiet=bool(args.quiet_progress),
        )
        for index, entry in enumerate(checks, start=1):
            # Явные индексы отделяют публичное имя от ссылки на функцию.
            # Вложенная распаковка enumerate теряет это различие в CodeQL.
            name = entry[0]
            fn = entry[1]
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                failures += 1
                result_items.append(
                    {
                        "check": "__timeout__",
                        "ok": False,
                        "details": f"Regression timeout reached before {index}/{total} {name}",
                        "duration_sec": round(time.time() - started, 3),
                    }
                )
                _print_progress(
                    f"[regression] timeout before {index}/{total} {name}",
                    quiet=bool(args.quiet_progress),
                )
                break
            check_root = os.path.join(temp_root, name)
            Path(check_root).mkdir(parents=True, exist_ok=True)
            check_started = time.time()
            _print_progress(
                f"[regression] {index}/{total} {name} start",
                quiet=bool(args.quiet_progress),
            )
            try:
                ok, details = fn(check_root)
            except Exception as exc:
                ok = False
                details = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=20)}"
            finally:
                cleanup_errors = _cleanup_check_resources()
            if cleanup_errors:
                ok = False
                details = f"{details}; resource cleanup failed: {'; '.join(cleanup_errors)}"
            duration_sec = round(time.time() - check_started, 3)
            result_items.append(
                {
                    "check": name,
                    "ok": bool(ok),
                    "details": str(details),
                    "duration_sec": duration_sec,
                }
            )
            if not ok:
                failures += 1
            status = "ok" if ok else "FAIL"
            _print_progress(
                f"[regression] {index}/{total} {name} {status} {duration_sec:.3f}s",
                quiet=bool(args.quiet_progress),
            )
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                failures += 1
                result_items.append(
                    {
                        "check": "__timeout__",
                        "ok": False,
                        "details": f"Regression timeout reached after {index}/{total} {name}",
                        "duration_sec": round(time.time() - started, 3),
                    }
                )
                _print_progress(
                    f"[regression] timeout after {index}/{total} {name}",
                    quiet=bool(args.quiet_progress),
                )
                break
    finally:
        _cleanup_check_resources()
        _rmtree_regression_root(temp_root)
        if timeout_s > 0:
            faulthandler.cancel_dump_traceback_later()

    report = {
        "total": len(checks),
        "failed": failures,
        "passed": sum(1 for item in result_items if item.get("ok")),
        "duration_sec": round(time.time() - started, 3),
        "timed_out": any(item.get("check") == "__timeout__" for item in result_items),
        "completed": sum(1 for item in result_items if item.get("check") != "__timeout__"),
        "profile": "worker" if shard_index is not None else "exhaustive",
        "shards": int(shard_count or 1),
        "shard_index": shard_index,
        "coverage_complete": not any(item.get("check") == "__timeout__" for item in result_items),
        "checks": result_items,
    }
    _emit_regression_report(
        report,
        json_detail=str(args.json_detail),
        report_path=str(args.report_path or ""),
    )
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
