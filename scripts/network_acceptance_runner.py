#!/usr/bin/env python
"""
Isolated network-folder acceptance runner for the shared SQLite workflow.

The runner creates a temporary Baza_rao3_jurnal tree, seeds one active
admission, runs multi-client write/read checks, then validates the resulting DB.
It never points tests at the user's configured production database.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = PROJECT_ROOT / "scripts"


@dataclass
class StepResult:
    name: str
    ok: bool
    duration_sec: float
    reason: str
    payload: dict[str, Any] | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    command: list[str] | None = None


def _tail(text: str, max_chars: int = 1600) -> str:
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _extract_last_json_dict(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    decoder = json.JSONDecoder()
    best_obj: dict[str, Any] | None = None
    best_end = -1
    for idx, char in enumerate(text):
        if char != "{":
            continue
        try:
            obj, end = decoder.raw_decode(text[idx:])
        except Exception:
            continue
        if isinstance(obj, dict) and idx + int(end) >= best_end:
            best_obj = obj
            best_end = idx + int(end)
    return best_obj


def _bootstrap_local_package():
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from _local_rem_card_bootstrap import bootstrap_local_rem_card

    bootstrap_local_rem_card()


def _isolated_env(temp_root: Path, baza_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    env["REMCARD_BAZA_DIR"] = str(baza_dir)
    env["LOCALAPPDATA"] = str(temp_root / "localappdata")
    env["REMCARD_LOCAL_LOGS_DIR"] = str(temp_root / "local_logs")
    env["REMCARD_LOCAL_FIRST_SYNC"] = "0"
    env["REMCARD_LOCAL_OUTBOX_SYNC"] = "0"
    env["REMCARD_LOCAL_SYNC_INTERVAL_SEC"] = "999"
    env["QT_QPA_PLATFORM"] = "offscreen"
    return env


def _run_subprocess(
    name: str,
    command: list[str],
    *,
    env: dict[str, str],
    timeout_sec: float,
    validate: Callable[[int, dict[str, Any] | None], tuple[bool, str]],
) -> StepResult:
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        payload = _extract_last_json_dict(proc.stdout) or _extract_last_json_dict(proc.stderr)
        ok, reason = validate(proc.returncode, payload)
        return StepResult(
            name=name,
            ok=ok,
            duration_sec=time.perf_counter() - started,
            reason=reason,
            payload=payload,
            stdout_tail=_tail(proc.stdout),
            stderr_tail=_tail(proc.stderr),
            command=command,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return StepResult(
            name=name,
            ok=False,
            duration_sec=time.perf_counter() - started,
            reason=f"timeout after {timeout_sec:.1f}s",
            stdout_tail=_tail(stdout),
            stderr_tail=_tail(stderr),
            command=command,
        )


def _validate_stress(expected_ops: int) -> Callable[[int, dict[str, Any] | None], tuple[bool, str]]:
    def _inner(exit_code: int, payload: dict[str, Any] | None) -> tuple[bool, str]:
        if exit_code != 0:
            return False, f"exit_code={exit_code}"
        if not payload:
            return False, "missing JSON payload"
        written_ok = int(payload.get("operations_written_ok", 0) or 0)
        writer_failed = int(payload.get("operations_writer_failed", 0) or 0)
        seen = int(payload.get("operations_reader_seen_by_change", 0) or 0)
        timed_out = int(payload.get("operations_reader_timed_out", 0) or 0)
        if writer_failed:
            return False, f"writer_failed={writer_failed}"
        if written_ok < expected_ops:
            return False, f"written_ok={written_ok} < expected={expected_ops}"
        if seen < expected_ops:
            return False, f"reader_seen={seen} < expected={expected_ops}"
        if timed_out:
            return False, f"reader_timed_out={timed_out}"
        return True, f"stress ok: written={written_ok}, seen={seen}"

    return _inner


def _validate_benchmark(expected_clicks: int) -> Callable[[int, dict[str, Any] | None], tuple[bool, str]]:
    def _inner(exit_code: int, payload: dict[str, Any] | None) -> tuple[bool, str]:
        if exit_code != 0:
            return False, f"exit_code={exit_code}"
        if not payload:
            return False, "missing JSON payload"
        if payload.get("ok") is False:
            return False, str(payload.get("error") or "benchmark returned ok=false")
        clicks = int(payload.get("clicks", 0) or 0)
        ui_samples = int(payload.get("ui_change_samples", 0) or 0)
        db_samples = int(payload.get("db_commit_samples", 0) or 0)
        if clicks != expected_clicks:
            return False, f"clicks={clicks} != expected={expected_clicks}"
        if ui_samples < expected_clicks:
            return False, f"ui_samples={ui_samples} < expected={expected_clicks}"
        if db_samples < expected_clicks:
            return False, f"db_samples={db_samples} < expected={expected_clicks}"
        return True, f"benchmark ok: ui={ui_samples}, db={db_samples}"

    return _inner


def _validate_restore_drill(exit_code: int, payload: dict[str, Any] | None) -> tuple[bool, str]:
    if exit_code != 0:
        return False, f"exit_code={exit_code}"
    if not payload:
        return False, "missing JSON payload"
    if payload.get("restore_drill_result") != "ok":
        return False, str(payload.get("error") or "restore drill failed")
    if payload.get("validation_status") != "ok":
        return False, f"validation_status={payload.get('validation_status')}"
    if not payload.get("restored_db_hash"):
        return False, "restored_db_hash is empty"
    return True, "restore drill ok"


def _validate_sqlite_file(db_path: Path) -> tuple[bool, str]:
    _bootstrap_local_package()
    from rem_card.app.sqlite_shared import configure_connection, run_integrity_check, run_quick_check

    if not db_path.exists():
        return False, f"database file missing: {db_path}"
    conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None, timeout=5.0)
    try:
        configure_connection(conn, profile="network")
        ok, reason = run_quick_check(conn)
        if not ok:
            return False, f"quick_check failed: {reason}"
        ok, reason = run_integrity_check(conn)
        if not ok:
            return False, f"integrity_check failed: {reason}"
        return True, "quick_check and integrity_check passed"
    finally:
        conn.close()


def _prepare_shared_baza(env: dict[str, str], baza_dir: Path) -> tuple[int, Path]:
    os.environ.update(
        {
            "REMCARD_BAZA_DIR": env["REMCARD_BAZA_DIR"],
            "LOCALAPPDATA": env["LOCALAPPDATA"],
            "REMCARD_LOCAL_LOGS_DIR": env["REMCARD_LOCAL_LOGS_DIR"],
            "REMCARD_LOCAL_FIRST_SYNC": env["REMCARD_LOCAL_FIRST_SYNC"],
            "REMCARD_LOCAL_OUTBOX_SYNC": env["REMCARD_LOCAL_OUTBOX_SYNC"],
            "QT_QPA_PLATFORM": env["QT_QPA_PLATFORM"],
        }
    )
    _bootstrap_local_package()

    from rem_card.app.bootstrap import bootstrap
    from rem_card.app.runtime_paths import create_baza_structure_and_db, get_journal_db_path
    from rem_card.services.patient_bed_management.service import PatientBedManagementService

    ok, reason = create_baza_structure_and_db(str(baza_dir))
    if not ok:
        raise RuntimeError(reason)

    container = bootstrap()
    try:
        with container.db_manager.remcard_transaction(source="network_acceptance_seed_beds") as cursor:
            for bed_number in range(1, 7):
                cursor.execute(
                    "INSERT OR IGNORE INTO beds (bed_number, status, current_admission_id) VALUES (?, 'FREE', NULL)",
                    (bed_number,),
                )

        manager = PatientBedManagementService(container.db_manager)
        admission_id = manager.create_patient_and_admission(
            {"full_name": "Тестовый Пациент Acceptance"},
            {
                "bed_number": 1,
                "history_number": f"ACC-{datetime.now().strftime('%Y%m%d%H%M%S')}",
                "admission_datetime": datetime.now() - timedelta(hours=1),
                "patient_age": 55,
                "patient_gender": "M",
                "diagnosis_text": "Acceptance test",
                "department_profile": "test",
            },
        )
        return int(admission_id), Path(get_journal_db_path(str(baza_dir)))
    finally:
        try:
            container.data_service.shutdown()
        except Exception:
            pass
        container.db_manager.close()


def _db_validation_step(name: str, db_path: Path) -> StepResult:
    started = time.perf_counter()
    ok, reason = _validate_sqlite_file(db_path)
    return StepResult(name=name, ok=ok, duration_sec=time.perf_counter() - started, reason=reason)


def _step_to_dict(step: StepResult) -> dict[str, Any]:
    return {
        "name": step.name,
        "ok": step.ok,
        "duration_sec": round(step.duration_sec, 3),
        "reason": step.reason,
        "payload": step.payload,
        "stdout_tail": step.stdout_tail,
        "stderr_tail": step.stderr_tail,
        "command": step.command,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run isolated network-folder acceptance checks")
    parser.add_argument("--operations", type=int, default=24, help="multi_client_stress operations")
    parser.add_argument("--benchmark-clicks", type=int, default=3, help="orders click benchmark clicks")
    parser.add_argument("--skip-benchmark", action="store_true", help="skip UI click benchmark")
    parser.add_argument("--timeout-s", type=float, default=240.0, help="timeout budget for each heavy subprocess")
    parser.add_argument("--keep-temp", action="store_true", help="keep temporary Baza_rao3_jurnal after completion")
    args = parser.parse_args()

    temp_root = Path(tempfile.mkdtemp(prefix="remcard_network_acceptance_")).resolve()
    baza_dir = temp_root / "network_share" / "Baza_rao3_jurnal"
    env = _isolated_env(temp_root, baza_dir)
    steps: list[StepResult] = []
    started = time.perf_counter()
    admission_id: int | None = None
    db_path: Path | None = None

    try:
        try:
            admission_id, db_path = _prepare_shared_baza(env, baza_dir)
            steps.append(StepResult("prepare_isolated_baza", True, 0.0, f"admission_id={admission_id}"))
        except Exception as exc:
            steps.append(StepResult("prepare_isolated_baza", False, 0.0, str(exc)))

        if db_path and steps[-1].ok:
            steps.append(_db_validation_step("pre_stress_db_validation", db_path))

        if admission_id and db_path and all(step.ok for step in steps):
            operations = max(1, int(args.operations))
            stress_command = [
                sys.executable,
                str(SCRIPT_DIR / "multi_client_stress.py"),
                "--admission-id",
                str(admission_id),
                "--operations",
                str(operations),
                "--writer-interval-ms",
                "80",
                "--reader-poll-ms",
                "100",
                "--sync-timeout-s",
                "5",
                "--max-runtime-s",
                str(max(30.0, float(args.timeout_s) - 30.0)),
            ]
            steps.append(
                _run_subprocess(
                    "multi_client_stress",
                    stress_command,
                    env=env,
                    timeout_sec=float(args.timeout_s),
                    validate=_validate_stress(operations),
                )
            )
            steps.append(_db_validation_step("post_stress_db_validation", db_path))

        if not args.skip_benchmark and admission_id and db_path and all(step.ok for step in steps):
            clicks = max(1, int(args.benchmark_clicks))
            benchmark_command = [
                sys.executable,
                str(SCRIPT_DIR / "orders_click_latency_benchmark.py"),
                "--clicks",
                str(clicks),
                "--max-runtime-s",
                "60",
                "--hard-timeout-s",
                str(max(70.0, float(args.timeout_s))),
            ]
            steps.append(
                _run_subprocess(
                    "orders_click_latency_benchmark",
                    benchmark_command,
                    env=env,
                    timeout_sec=max(80.0, float(args.timeout_s) + 10.0),
                    validate=_validate_benchmark(clicks),
                )
            )
            steps.append(_db_validation_step("post_benchmark_db_validation", db_path))

        if admission_id and db_path and all(step.ok for step in steps):
            restore_command = [
                sys.executable,
                str(SCRIPT_DIR / "restore_drill.py"),
                "--backup-dir",
                str(baza_dir / "backups" / "valid"),
                "--report-dir",
                str(baza_dir / "backup_health" / "reports"),
                "--restore-root",
                str(baza_dir / "backup_health" / "restore_drills"),
                "--max-files",
                "20",
                "--cleanup-restored",
            ]
            steps.append(
                _run_subprocess(
                    "restore_drill",
                    restore_command,
                    env=env,
                    timeout_sec=90.0,
                    validate=_validate_restore_drill,
                )
            )
    finally:
        keep_temp = bool(args.keep_temp)
        if not keep_temp:
            shutil.rmtree(temp_root, ignore_errors=True)

    failed = [step for step in steps if not step.ok]
    report = {
        "status": "failed" if failed else "passed",
        "duration_sec": round(time.perf_counter() - started, 3),
        "temp_root": str(temp_root),
        "temp_root_kept": bool(args.keep_temp),
        "baza_dir": str(baza_dir),
        "db_path": str(db_path) if db_path else None,
        "admission_id": admission_id,
        "checks_total": len(steps),
        "checks_passed": len(steps) - len(failed),
        "checks_failed": len(failed),
        "checks": [_step_to_dict(step) for step in steps],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
