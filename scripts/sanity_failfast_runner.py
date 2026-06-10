#!/usr/bin/env python
"""
Fail-fast sanity runner for quality, stress and benchmark safety checks.

Runs a short sequence:
1) code_quality_checks.py
2) regression_safety_checks.py
3) multi_client_stress.py
4) orders_click_latency_benchmark.py

Stops on first failure and writes a JSON report.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DEFAULT_REPORT_DIR = SCRIPT_DIR / "bench_results" / "sanity_runs"


@dataclass
class CheckResult:
    name: str
    ok: bool
    duration_sec: float
    exit_code: int | None
    reason: str
    json_payload: dict[str, Any] | None
    stdout_tail: str
    stderr_tail: str
    command: list[str]


def _extract_last_json_dict(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    decoder = json.JSONDecoder()
    best_obj: dict[str, Any] | None = None
    best_end = -1
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, end = decoder.raw_decode(text[idx:])
        except Exception:
            continue
        if isinstance(obj, dict):
            absolute_end = idx + int(end)
            if absolute_end >= best_end:
                best_end = absolute_end
                best_obj = obj
    return best_obj


def _tail(text: str, max_chars: int = 1600) -> str:
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _terminate_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _read_pipe(pipe, stream_name: str, output_queue: queue.Queue[tuple[str, str]]) -> None:
    try:
        for line in iter(pipe.readline, ""):
            output_queue.put((stream_name, line))
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def _run_check(
    *,
    name: str,
    command: list[str],
    timeout_sec: float,
    idle_timeout_sec: float | None = None,
    env: dict[str, str],
    validate: Callable[[int, dict[str, Any] | None], tuple[bool, str]],
) -> CheckResult:
    started = time.perf_counter()
    timeout_sec = float(timeout_sec or 0.0)
    idle_timeout_sec = float(idle_timeout_sec or 0.0)
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    output_queue: queue.Queue[tuple[str, str]] = queue.Queue()
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
    readers = [
        threading.Thread(target=_read_pipe, args=(proc.stdout, "stdout", output_queue), daemon=True),
        threading.Thread(target=_read_pipe, args=(proc.stderr, "stderr", output_queue), daemon=True),
    ]
    for reader in readers:
        reader.start()

    last_output = started
    timed_out_reason = ""
    while True:
        try:
            stream_name, line = output_queue.get(timeout=0.1)
            if stream_name == "stdout":
                stdout_parts.append(line)
            else:
                stderr_parts.append(line)
            last_output = time.perf_counter()
        except queue.Empty:
            pass

        if proc.poll() is not None:
            break

        now = time.perf_counter()
        if timeout_sec > 0 and now - started >= timeout_sec:
            timed_out_reason = f"Timeout after {timeout_sec:.1f}s"
            _terminate_process(proc)
            break
        if idle_timeout_sec > 0 and now - last_output >= idle_timeout_sec:
            timed_out_reason = f"Idle timeout after {idle_timeout_sec:.1f}s without output"
            _terminate_process(proc)
            break

    for reader in readers:
        reader.join(timeout=2.0)
    while True:
        try:
            stream_name, line = output_queue.get_nowait()
            if stream_name == "stdout":
                stdout_parts.append(line)
            else:
                stderr_parts.append(line)
        except queue.Empty:
            break

    duration = time.perf_counter() - started
    stdout = "".join(stdout_parts)
    stderr = "".join(stderr_parts)
    payload = _extract_last_json_dict(stdout) or _extract_last_json_dict(stderr)
    if timed_out_reason:
        duration = time.perf_counter() - started
        return CheckResult(
            name=name,
            ok=False,
            duration_sec=duration,
            exit_code=None,
            reason=timed_out_reason,
            json_payload=payload,
            stdout_tail=_tail(stdout),
            stderr_tail=_tail(stderr),
            command=command,
        )
    ok, reason = validate(proc.returncode or 0, payload)
    return CheckResult(
        name=name,
        ok=ok,
        duration_sec=duration,
        exit_code=proc.returncode,
        reason=reason,
        json_payload=payload,
        stdout_tail=_tail(stdout),
        stderr_tail=_tail(stderr),
        command=command,
    )


def _validate_regression(exit_code: int, payload: dict[str, Any] | None) -> tuple[bool, str]:
    if exit_code != 0:
        return False, f"Process exit code {exit_code}"
    if not payload:
        return False, "Missing JSON payload"
    total = int(payload.get("total", 0) or 0)
    failed = int(payload.get("failed", 0) or 0)
    passed = int(payload.get("passed", 0) or 0)
    if total <= 0:
        return False, "Regression payload has zero checks"
    if failed != 0 or passed != total:
        return False, f"Regression mismatch: passed={passed}, failed={failed}, total={total}"
    return True, f"Passed {passed}/{total}"


def _validate_quality(exit_code: int, payload: dict[str, Any] | None) -> tuple[bool, str]:
    if exit_code != 0:
        return False, f"Process exit code {exit_code}"
    if not payload:
        return False, "Missing JSON payload"
    status = str(payload.get("status") or "")
    failed = int(payload.get("checks_failed", 0) or 0)
    passed = int(payload.get("checks_passed", 0) or 0)
    total = int(payload.get("checks_total", 0) or 0)
    if status != "passed" or failed:
        return False, f"Quality checks failed: passed={passed}, failed={failed}, total={total}"
    return True, f"Quality OK: passed={passed}/{total}"


def _validate_architecture(exit_code: int, payload: dict[str, Any] | None) -> tuple[bool, str]:
    if exit_code != 0:
        return False, f"Process exit code {exit_code}"
    if not payload:
        return False, "Missing JSON payload"
    status = str(payload.get("status") or "")
    failed = int(payload.get("checks_failed", 0) or 0)
    passed = int(payload.get("checks_passed", 0) or 0)
    total = int(payload.get("checks_total", 0) or 0)
    if status != "passed" or failed:
        return False, f"Architecture checks failed: passed={passed}, failed={failed}, total={total}"
    return True, f"Architecture OK: passed={passed}/{total}"


def _validate_stress(expected_ops: int) -> Callable[[int, dict[str, Any] | None], tuple[bool, str]]:
    def _inner(exit_code: int, payload: dict[str, Any] | None) -> tuple[bool, str]:
        if exit_code != 0:
            return False, f"Process exit code {exit_code}"
        if not payload:
            return False, "Missing JSON payload"

        written_ok = int(payload.get("operations_written_ok", 0) or 0)
        writer_failed = int(payload.get("operations_writer_failed", 0) or 0)
        seen = int(payload.get("operations_reader_seen_by_change", 0) or 0)
        timed_out = int(payload.get("operations_reader_timed_out", 0) or 0)
        writer_done = bool(payload.get("writer_reported_done", False))
        writer_exit_code = payload.get("writer_exit_code")

        if not writer_done:
            return False, "Writer did not report completion"
        if writer_exit_code not in (0, None):
            return False, f"Writer exit code is {writer_exit_code}"
        if writer_failed != 0:
            return False, f"Writer failed operations: {writer_failed}"
        if written_ok < expected_ops:
            return False, f"Written operations below expected ({written_ok} < {expected_ops})"
        if seen < expected_ops:
            return False, f"Reader seen-by-change below expected ({seen} < {expected_ops})"
        if timed_out != 0:
            return False, f"Reader timed out events: {timed_out}"
        return True, f"Stress OK: written={written_ok}, seen={seen}"

    return _inner


def _validate_click_benchmark(expected_clicks: int) -> Callable[[int, dict[str, Any] | None], tuple[bool, str]]:
    def _inner(exit_code: int, payload: dict[str, Any] | None) -> tuple[bool, str]:
        if exit_code != 0:
            return False, f"Process exit code {exit_code}"
        if not payload:
            return False, "Missing JSON payload"
        if payload.get("ok") is False:
            return False, f"Benchmark returned error: {payload.get('error', 'unknown')}"

        clicks = int(payload.get("clicks", 0) or 0)
        ui_samples = int(payload.get("ui_change_samples", 0) or 0)
        db_samples = int(payload.get("db_commit_samples", 0) or 0)
        if clicks != expected_clicks:
            return False, f"Unexpected clicks count in payload ({clicks} != {expected_clicks})"
        if ui_samples < expected_clicks:
            return False, f"UI samples below expected ({ui_samples} < {expected_clicks})"
        if db_samples < expected_clicks:
            return False, f"DB samples below expected ({db_samples} < {expected_clicks})"
        return True, f"Benchmark OK: ui={ui_samples}, db={db_samples}"

    return _inner


def _validate_network_acceptance(exit_code: int, payload: dict[str, Any] | None) -> tuple[bool, str]:
    if exit_code != 0:
        return False, f"Process exit code {exit_code}"
    if not payload:
        return False, "Missing JSON payload"
    status = str(payload.get("status") or "")
    failed = int(payload.get("checks_failed", 0) or 0)
    passed = int(payload.get("checks_passed", 0) or 0)
    total = int(payload.get("checks_total", 0) or 0)
    if status != "passed" or failed:
        return False, f"Network acceptance failed: passed={passed}, failed={failed}, total={total}"
    return True, f"Network acceptance OK: passed={passed}/{total}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail-fast sanity stress runner with JSON report")
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR), help="Directory where JSON report is written")
    parser.add_argument("--python", default=sys.executable, help="Python executable")
    parser.add_argument("--stress-operations", type=int, default=20, help="Operations for multi_client_stress")
    parser.add_argument("--stress-timeout-s", type=float, default=180.0, help="Timeout for stress script")
    parser.add_argument("--benchmark-clicks", type=int, default=5, help="Clicks for orders latency benchmark")
    parser.add_argument("--benchmark-timeout-s", type=float, default=120.0, help="Hard timeout for benchmark script")
    parser.add_argument("--quality-timeout-s", type=float, default=60.0, help="Timeout for static quality checks")
    parser.add_argument("--regression-timeout-s", type=float, default=600.0, help="Hard timeout for regression checks")
    parser.add_argument(
        "--regression-idle-timeout-s",
        type=float,
        default=180.0,
        help="Timeout for regression checks when no progress output is received",
    )
    args = parser.parse_args()

    report_dir = Path(args.report_dir).resolve()
    report_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")

    checks_plan = [
        {
            "name": "code_quality_checks",
            "command": [args.python, str(SCRIPT_DIR / "code_quality_checks.py")],
            "timeout": float(args.quality_timeout_s),
            "validate": _validate_quality,
        },
        {
            "name": "architecture_safety_check",
            "command": [args.python, str(SCRIPT_DIR / "architecture_safety_check.py")],
            "timeout": float(args.quality_timeout_s),
            "validate": _validate_architecture,
        },
        {
            "name": "regression_safety_checks",
            "command": [
                args.python,
                str(SCRIPT_DIR / "regression_safety_checks.py"),
                "--timeout-s",
                str(max(0.0, float(args.regression_timeout_s) - 5.0)),
            ],
            "timeout": float(args.regression_timeout_s),
            "idle_timeout": float(args.regression_idle_timeout_s),
            "validate": _validate_regression,
        },
        {
            "name": "network_acceptance_runner",
            "command": [
                args.python,
                str(SCRIPT_DIR / "network_acceptance_runner.py"),
                "--operations",
                str(max(1, int(args.stress_operations))),
                "--benchmark-clicks",
                str(max(1, int(args.benchmark_clicks))),
                "--timeout-s",
                str(max(float(args.stress_timeout_s), float(args.benchmark_timeout_s))),
            ],
            "timeout": max(float(args.stress_timeout_s), float(args.benchmark_timeout_s)) + 80.0,
            "validate": _validate_network_acceptance,
        },
    ]

    results: list[CheckResult] = []
    failed = False
    failure_reason = ""
    started_at = datetime.now()
    started_perf = time.perf_counter()

    for item in checks_plan:
        result = _run_check(
            name=item["name"],
            command=item["command"],
            timeout_sec=item["timeout"],
            idle_timeout_sec=item.get("idle_timeout"),
            env=env,
            validate=item["validate"],
        )
        results.append(result)
        if not result.ok:
            failed = True
            failure_reason = f"{result.name}: {result.reason}"
            break

    duration_total = time.perf_counter() - started_perf
    finished_at = datetime.now()

    report = {
        "status": "failed" if failed else "passed",
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_sec": round(duration_total, 3),
        "failure_reason": failure_reason,
        "checks_total": len(checks_plan),
        "checks_executed": len(results),
        "checks_passed": sum(1 for r in results if r.ok),
        "checks_failed": sum(1 for r in results if not r.ok),
        "checks": [
            {
                "name": r.name,
                "ok": r.ok,
                "duration_sec": round(r.duration_sec, 3),
                "exit_code": r.exit_code,
                "reason": r.reason,
                "command": r.command,
                "json_payload": r.json_payload,
                "stdout_tail": r.stdout_tail,
                "stderr_tail": r.stderr_tail,
            }
            for r in results
        ],
    }

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = report_dir / f"sanity_failfast_{stamp}.json"
    latest_path = report_dir / "sanity_failfast_latest.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    latest_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        json.dumps(
            {
                "status": report["status"],
                "failure_reason": report["failure_reason"],
                "checks_executed": report["checks_executed"],
                "checks_passed": report["checks_passed"],
                "checks_failed": report["checks_failed"],
                "report_path": str(report_path),
                "latest_path": str(latest_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
