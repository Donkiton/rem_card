"""Одинаковые изолированные команды и отчёты для CI и локального запуска."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from scripts.ci.test_groups import PROJECT_ROOT, load_groups


def run_command(command: list[str], *, log: Path, env: dict[str, str], timeout: float) -> dict:
    started = time.monotonic()
    timed_out = False
    print(f"Запуск: {' '.join(command)}", flush=True)
    with log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(command, cwd=PROJECT_ROOT, env=env, stdout=output, stderr=subprocess.STDOUT)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
            else:
                process.kill()
            process.wait()
    text = log.read_text(encoding="utf-8", errors="replace")
    print("\n".join(text.splitlines()[-100:]), flush=True)
    return {
        "command": command,
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "duration_sec": round(time.monotonic() - started, 3),
        "ok": not timed_out and process.returncode == 0,
        "log": log.name,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=("core", "ui", "all", "quality", "regression"))
    parser.add_argument("--report-dir", type=Path, default=PROJECT_ROOT / "tmp" / "ci-reports")
    parser.add_argument("--timeout-s", type=float, default=900)
    args = parser.parse_args(argv)
    load_groups()
    report_dir = args.report_dir.resolve() / args.suite
    report_dir.mkdir(parents=True, exist_ok=True)
    temp_parent = PROJECT_ROOT / "tmp"
    temp_parent.mkdir(exist_ok=True)
    py = sys.executable
    if args.suite == "quality":
        commands = [
            ("quality", [py, "scripts/code_quality_checks.py"]),
            ("architecture", [py, "scripts/architecture_safety_check.py"]),
        ]
    elif args.suite == "regression":
        commands = [("regression", [
            py, "scripts/regression_safety_checks.py", "--profile", "fast", "--jobs", "4",
            "--timeout-s", str(args.timeout_s), "--quiet-progress", "--json-detail", "summary",
            "--report-path", str(report_dir / "regression.json"),
        ])]
    else:
        command = [
            py, "-m", "pytest", "tests", "-q", "--durations=25", "--durations-min=0.5",
            f"--junitxml={report_dir / 'junit.xml'}",
            f"--collection-report={report_dir / 'collected.json'}",
        ]
        if args.suite != "all":
            command.append(f"--ci-group={args.suite}")
        commands = [("pytest", command)]
    with tempfile.TemporaryDirectory(prefix="rc_ci_", dir=temp_parent) as temp:
        env = dict(os.environ)
        env.update({
            "PYTHONUTF8": "1", "QT_QPA_PLATFORM": "offscreen",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "REMCARD_BAZA_DIR": str(Path(temp) / "baza"),
            "REMCARD_CI_SETTINGS_DIR": str(Path(temp) / "settings"),
        })
        results = [
            run_command(command, log=report_dir / f"{name}.log", env=env, timeout=args.timeout_s)
            for name, command in commands
        ]
    report = {"suite": args.suite, "ok": all(item["ok"] for item in results), "commands": results}
    if args.suite == "regression" and report["ok"]:
        safety = json.loads((report_dir / "regression.json").read_text(encoding="utf-8"))
        if safety.get("native_crash_retries", 0):
            report["ok"] = False
            report["error"] = "Native crash потребовал повторного запуска; отчёт сохранён для диагностики."
            print(report["error"], flush=True)
    (report_dir / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as output:
            output.write(f"### {args.suite}\n\n| Проверка | Результат | Секунды |\n|---|---|---:|\n")
            for name, result in zip((name for name, _ in commands), results):
                output.write(f"| {name} | {'Успех' if result['ok'] else 'Ошибка'} | {result['duration_sec']} |\n")
    return 0 if report["ok"] else 1
