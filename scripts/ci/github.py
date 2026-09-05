"""Планирование повторных событий PR и итоговый обязательный статус tests."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import zipfile
from pathlib import Path

from scripts.ci.test_groups import load_groups


SUITES = ("quality", "core", "ui", "regression")


def gh_json(path: str) -> dict:
    result = subprocess.run(["gh", "api", path], capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(result.stdout)


def proof_matches(proof: dict, sha: str) -> bool:
    return (
        proof.get("schema") == 1
        and proof.get("checked_sha") == sha
        and proof.get("suites") == list(SUITES)
        and type(proof.get("test_count")) is int
        and proof["test_count"] > 0
        and type(proof.get("regression_count")) is int
        and proof["regression_count"] > 0
        and all(
            isinstance(proof.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", proof[key]) is not None
            for key in ("pytest_manifest_sha256", "regression_manifest_sha256")
        )
    )


def metadata_only(event: dict, event_name: str) -> bool:
    changes = event.get("changes", {})
    return (
        event_name == "pull_request" and event.get("action") == "edited"
        and bool(changes) and set(changes) <= {"title", "body"}
    )


def find_verified_run(repository: str, head_sha: str, checked_sha: str) -> str:
    runs = gh_json(f"repos/{repository}/actions/workflows/tests.yml/runs?per_page=30&head_sha={head_sha}")
    for run in runs.get("workflow_runs", []):
        if run.get("conclusion") != "success" or run.get("head_sha") != head_sha or run.get("event") != "pull_request":
            continue
        artifacts = gh_json(f"repos/{repository}/actions/runs/{run['id']}/artifacts")
        for artifact in artifacts.get("artifacts", []):
            if artifact.get("name") != "ci-gate" or artifact.get("expired"):
                continue
            archive = subprocess.run(
                ["gh", "api", f"repos/{repository}/actions/artifacts/{artifact['id']}/zip"],
                capture_output=True, check=True,
            ).stdout
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                proof = json.loads(bundle.read("proof.json"))
            if proof_matches(proof, checked_sha):
                return str(run["id"])
    return ""


def plan(sha: str) -> None:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    verified = ""
    if metadata_only(event, os.environ.get("GITHUB_EVENT_NAME", "")):
        try:
            verified = find_verified_run(os.environ["GITHUB_REPOSITORY"], event["pull_request"]["head"]["sha"], sha)
        except (OSError, ValueError, KeyError, subprocess.CalledProcessError, zipfile.BadZipFile) as exc:
            # Недоступный или повреждённый отчёт никогда не разрешает пропуск.
            print(f"Не удалось подтвердить прошлый результат ({type(exc).__name__}); выполняем все проверки.")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"reuse={'true' if verified else 'false'}\nverified_run={verified}\n")
    print(f"Подтверждённый запуск: {verified}" if verified else "Требуется полный запуск всех групп.")


def validate_needs(needs: dict, *, reuse: bool) -> None:
    if needs.get("plan", {}).get("result") != "success":
        raise ValueError("Планирование проверок не завершилось успешно")
    if reuse:
        if not needs["plan"].get("outputs", {}).get("verified_run"):
            raise ValueError("Нет подтверждения предыдущего запуска")
        if any(needs.get(suite, {}).get("result") != "skipped" for suite in SUITES):
            raise ValueError("Повторное использование не может перекрывать результаты текущих групп")
    elif any(needs.get(suite, {}).get("result") != "success" for suite in SUITES):
        raise ValueError("Не все обязательные группы завершились успешно")


def build_proof(reports: Path, sha: str) -> dict:
    from scripts.regression_checks.registry import get_checks

    groups = load_groups()
    collected = []
    for suite in SUITES:
        result = json.loads((reports / suite / "result.json").read_text(encoding="utf-8"))
        if result.get("suite") != suite or result.get("ok") is not True:
            raise ValueError(f"Некорректный или неуспешный отчёт {suite}")
        if not result.get("commands") or any(command.get("ok") is not True for command in result["commands"]):
            raise ValueError(f"Не все команды {suite} завершились успешно")
        if suite in ("core", "ui"):
            nodes = json.loads((reports / suite / "collected.json").read_text(encoding="utf-8"))
            if not nodes or any(groups.get(node.split("::", 1)[0]) != suite for node in nodes):
                raise ValueError(f"Состав тестов не соответствует группе {suite}")
            collected.extend(nodes)
    if len(collected) != len(set(collected)) or {node.split("::", 1)[0] for node in collected} != set(groups):
        raise ValueError("Тесты пропущены или выполнены в нескольких группах")
    regression = json.loads((reports / "regression" / "regression.json").read_text(encoding="utf-8"))
    checks = regression.get("checks", [])
    names = [check["check"] for check in checks]
    if (
        not names or len(names) != len(set(names)) or regression.get("coverage_complete") is not True
        or regression.get("failed") != 0 or regression.get("total") != len(names)
        or regression.get("completed") != len(names) or not all(check.get("ok") is True for check in checks)
        or regression.get("native_crash_retries", 0)
        or names != [name for name, _ in get_checks()]
    ):
        raise ValueError("Safety-проверки неполны, завершились ошибкой или потребовали повтора после native crash")
    def digest(items):
        return hashlib.sha256("\n".join(sorted(items)).encode("utf-8")).hexdigest()
    return {
        "schema": 1, "checked_sha": sha, "suites": list(SUITES),
        "test_count": len(collected), "pytest_manifest_sha256": digest(collected),
        "regression_count": len(names), "regression_manifest_sha256": digest(names),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "gate"))
    args = parser.parse_args()
    sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    if args.mode == "plan":
        plan(sha)
        return
    needs = json.loads(os.environ["CI_NEEDS_JSON"])
    reuse = needs.get("plan", {}).get("outputs", {}).get("reuse") == "true"
    validate_needs(needs, reuse=reuse)
    if reuse:
        proof = json.loads(Path("tmp/ci-proof/proof.json").read_text(encoding="utf-8"))
        if not proof_matches(proof, sha):
            raise ValueError("Предыдущий результат относится к другой ревизии или неполному набору проверок")
    else:
        proof = build_proof(Path("tmp/ci-reports"), sha)
    target = Path("tmp/ci-proof/proof.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(proof, indent=2), encoding="utf-8")
    summary = f"Проверено: {proof['test_count']} тестов, {proof['regression_count']} safety-сценариев; ревизия {sha}."
    print(summary)
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as output:
        output.write(summary + "\n")


if __name__ == "__main__":
    main()
