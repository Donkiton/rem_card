#!/usr/bin/env python
"""
Fast static quality gate for RemCard.

Rules:
- F821 is forbidden everywhere.
- UTF-8 BOM is forbidden everywhere.
- New F-ranked cyclomatic-complexity blocks are forbidden.

Existing F-ranked blocks are tracked as a temporary baseline and should be
refactored in separate, focused tasks.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKIP_DIR_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".remcard",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "tmp",
    "venv",
}

# Existing F-ranked blocks and their maximum accepted complexity. This allows
# gradual cleanup while blocking both new F-ranked blocks and baseline growth.
ALLOWED_F_BLOCKS = {}


def _rel(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


def _is_skipped(path: Path) -> bool:
    return any(part in SKIP_DIR_NAMES for part in path.parts)


def _project_files():
    # Исключаем виртуальное окружение и сборки до обхода их содержимого.
    for directory, dirs, files in os.walk(PROJECT_ROOT):
        dirs[:] = sorted(name for name in dirs if name not in SKIP_DIR_NAMES)
        for name in sorted(files):
            path = Path(directory, name)
            if not _is_skipped(path) and path.is_file():
                yield path


def _run_flake8_static_analysis() -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "flake8",
        ".",
        "--select=F401,F402,F541,F811,F821,F841",
        "--exclude=.git,__pycache__,build,dist,tmp,.venv,venv,.pytest_cache,.mypy_cache,.remcard,.ruff_cache",
    ]
    started = time.perf_counter()
    proc = subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    return {
        "name": "flake8_static_analysis",
        "ok": proc.returncode == 0,
        "duration_sec": round(time.perf_counter() - started, 3),
        "command": command,
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def _scan_bom() -> dict[str, Any]:
    started = time.perf_counter()
    offenders: list[str] = []
    for path in _project_files():
        try:
            with path.open("rb") as handle:
                if handle.read(3) == b"\xef\xbb\xbf":
                    offenders.append(_rel(path))
        except OSError as exc:
            offenders.append(f"{_rel(path)} (read error: {exc})")

    return {
        "name": "bom_scan",
        "ok": not offenders,
        "duration_sec": round(time.perf_counter() - started, 3),
        "offenders": offenders,
    }


def _scan_complexity_f() -> dict[str, Any]:
    started = time.perf_counter()
    try:
        from radon.complexity import cc_rank, cc_visit
    except Exception as exc:
        return {
            "name": "radon_cc_min_f",
            "ok": False,
            "duration_sec": round(time.perf_counter() - started, 3),
            "error": f"radon is unavailable: {exc}",
            "blocks": [],
            "new_f_blocks": [],
            "worsened_f_blocks": [],
            "missing_baseline_blocks": [],
        }

    blocks: list[dict[str, Any]] = []
    parse_errors: list[dict[str, str]] = []
    for path in _project_files():
        if path.suffix != ".py":
            continue
        rel_path = _rel(path)
        try:
            source = path.read_text(encoding="utf-8")
            visited = cc_visit(source)
        except Exception as exc:
            parse_errors.append({"path": rel_path, "error": str(exc)})
            continue

        for block in visited:
            if cc_rank(block.complexity) != "F":
                continue
            fullname = getattr(block, "fullname", block.name)
            blocks.append(
                {
                    "path": rel_path,
                    "name": fullname,
                    "line": int(block.lineno),
                    "complexity": int(block.complexity),
                }
            )

    found = {(item["path"], item["name"]) for item in blocks}
    new_f_blocks = [
        item
        for item in blocks
        if (item["path"], item["name"]) not in ALLOWED_F_BLOCKS
    ]
    worsened_f_blocks = [
        {
            **item,
            "allowed_complexity": ALLOWED_F_BLOCKS[(item["path"], item["name"])],
        }
        for item in blocks
        if (
            (item["path"], item["name"]) in ALLOWED_F_BLOCKS
            and item["complexity"] > ALLOWED_F_BLOCKS[(item["path"], item["name"])]
        )
    ]
    missing_baseline_blocks = [
        {"path": path, "name": name}
        for path, name in sorted(ALLOWED_F_BLOCKS.keys() - found)
    ]

    return {
        "name": "radon_cc_min_f",
        "ok": (
            not parse_errors
            and not new_f_blocks
            and not worsened_f_blocks
            and not missing_baseline_blocks
        ),
        "duration_sec": round(time.perf_counter() - started, 3),
        "equivalent_command": [sys.executable, "-m", "radon", "cc", ".", "-s", "--min", "F"],
        "blocks": blocks,
        "new_f_blocks": new_f_blocks,
        "worsened_f_blocks": worsened_f_blocks,
        "missing_baseline_blocks": missing_baseline_blocks,
        "parse_errors": parse_errors,
    }


def main() -> int:
    checks = [
        _run_flake8_static_analysis(),
        _scan_bom(),
        _scan_complexity_f(),
    ]
    failed = [check for check in checks if not check.get("ok")]
    report = {
        "status": "failed" if failed else "passed",
        "checks_total": len(checks),
        "checks_passed": len(checks) - len(failed),
        "checks_failed": len(failed),
        "checks": checks,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
