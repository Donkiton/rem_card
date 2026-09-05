"""Обновить оценки времени по успешному полному JSON-отчёту safety-проверок."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.regression_checks.registry import get_checks  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    checks = report.get("checks", [])
    expected = [name for name, _ in get_checks()]
    if (
        report.get("failed") != 0 or report.get("coverage_complete") is not True
        or report.get("native_crash_retries", 0)
        or [item["check"] for item in checks] != expected
        or not all(item.get("ok") is True for item in checks)
    ):
        raise SystemExit("Нужен полный успешный отчёт без native crash и повторов")
    durations = {item["check"]: float(item["duration_sec"]) for item in checks}
    if any(not math.isfinite(value) or value < 0 for value in durations.values()):
        raise SystemExit("Некорректные измерения времени")
    target = Path(__file__).with_name("regression_checks") / "timing_estimates.json"
    payload = {
        "schema": 1, "source": f"Full successful report: {args.report.name}",
        "durations_sec": {name: max(0.01, round(value, 3)) for name, value in durations.items()},
    }
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Обновлено {len(durations)} оценок в {target}")


if __name__ == "__main__":
    main()
