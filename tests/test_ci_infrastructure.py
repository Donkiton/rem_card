from __future__ import annotations

import json
import os
import sys

import pytest

from scripts.ci import github, runner
from scripts.ci.test_groups import load_groups


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_group_inventory_rejects_missing_duplicate_and_stale_modules(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_logic.py").write_text("", encoding="utf-8")
    (tests / "test_window.py").write_text("", encoding="utf-8")
    valid = {"core": ["tests/test_logic.py"], "ui": ["tests/test_window.py"]}
    write_json(tests / "groups.json", valid)
    assert load_groups(tmp_path) == {"tests/test_logic.py": "core", "tests/test_window.py": "ui"}
    for invalid in (
        {"core": ["tests/test_logic.py"], "ui": ["tests/test_logic.py"]},
        {"core": ["tests/test_logic.py"], "ui": ["tests/test_absent.py"]},
    ):
        write_json(tests / "groups.json", invalid)
        with pytest.raises(ValueError):
            load_groups(tmp_path)
    write_json(tests / "groups.json", valid)
    (tests / "test_new.py").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="test_new"):
        load_groups(tmp_path)


@pytest.mark.parametrize("changes,eligible", [
    ({"title": {"from": "old"}}, True),
    ({"body": {"from": "old"}}, True),
    ({"base": {"ref": {"from": "old"}}}, False),
    ({"title": {}, "base": {}}, False),
    ({}, False),
])
def test_only_metadata_edits_can_reuse_a_verified_result(changes, eligible):
    event = {"action": "edited", "changes": changes}
    assert github.metadata_only(event, "pull_request") is eligible
    assert not github.metadata_only(event, "push")
    event["action"] = "synchronize"
    assert not github.metadata_only(event, "pull_request")


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", "", None])
def test_required_gate_rejects_every_incomplete_group(result):
    needs = {name: {"result": "success"} for name in ("plan", *github.SUITES)}
    github.validate_needs(needs, reuse=False)
    for suite in ("plan", *github.SUITES):
        changed = {name: dict(value) for name, value in needs.items()}
        changed[suite]["result"] = result
        with pytest.raises(ValueError):
            github.validate_needs(changed, reuse=False)


def test_reuse_requires_proof_and_cannot_hide_a_new_failure():
    needs = {name: {"result": "skipped"} for name in github.SUITES}
    needs["plan"] = {"result": "success", "outputs": {}}
    with pytest.raises(ValueError):
        github.validate_needs(needs, reuse=True)
    needs["plan"]["outputs"]["verified_run"] = "123"
    github.validate_needs(needs, reuse=True)
    needs["ui"]["result"] = "failure"
    with pytest.raises(ValueError):
        github.validate_needs(needs, reuse=True)


def test_reuse_proof_requires_exact_revision_and_all_suites():
    proof = {
        "schema": 1, "checked_sha": "abc", "suites": list(github.SUITES),
        "test_count": 2, "regression_count": 538,
        "pytest_manifest_sha256": "a" * 64, "regression_manifest_sha256": "b" * 64,
    }
    assert github.proof_matches(proof, "abc")
    assert not github.proof_matches(proof, "def")
    for key, value in [("suites", ["core"]), ("test_count", 0), ("test_count", True), ("regression_manifest_sha256", "x" * 64)]:
        assert not github.proof_matches({**proof, key: value}, "abc")


def test_unavailable_previous_proof_falls_back_to_full_run(tmp_path, monkeypatch):
    event_path = tmp_path / "event.json"
    write_json(event_path, {"action": "edited", "changes": {"body": {}}, "pull_request": {"head": {"sha": "abc"}}})
    output = tmp_path / "outputs"
    for key, value in {"GITHUB_EVENT_PATH": str(event_path), "GITHUB_OUTPUT": str(output), "GITHUB_EVENT_NAME": "pull_request", "GITHUB_REPOSITORY": "owner/repo"}.items():
        monkeypatch.setenv(key, value)
    def unavailable(*_args):
        raise OSError("API unavailable")
    monkeypatch.setattr(github, "find_verified_run", unavailable)
    github.plan("abc")
    assert output.read_text(encoding="utf-8") == "reuse=false\nverified_run=\n"


def test_command_runner_reports_real_failure_and_hard_timeout(tmp_path):
    failure = runner.run_command([sys.executable, "-c", "print('intentional'); raise SystemExit(7)"], log=tmp_path / "failure.log", env=dict(os.environ), timeout=10)
    assert not failure["ok"] and failure["exit_code"] == 7 and not failure["timed_out"]
    assert "intentional" in (tmp_path / "failure.log").read_text(encoding="utf-8")
    timeout = runner.run_command([sys.executable, "-c", "import time; time.sleep(30)"], log=tmp_path / "timeout.log", env=dict(os.environ), timeout=0.3)
    assert timeout["timed_out"] and not timeout["ok"]


def test_pytest_command_selects_tests_directory_before_custom_options(tmp_path, monkeypatch):
    commands = []
    def capture(command, **kwargs):
        commands.append(command)
        return {"ok": True, "duration_sec": 0}
    monkeypatch.setattr(runner, "run_command", capture)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert runner.main(["ui", "--report-dir", str(tmp_path)]) == 0
    assert commands[0][3] == "tests"
    assert "--ci-group=ui" in commands[0]


def test_group_inventory_of_repository_is_complete():
    groups = load_groups()
    assert groups["tests/test_drug_dialog_geometry.py"] == "ui"
    assert groups["tests/test_sqlite_write_commit_retry.py"] == "core"


@pytest.fixture
def complete_reports(tmp_path, monkeypatch):
    from scripts.regression_checks import registry

    monkeypatch.setattr(github, "load_groups", lambda: {"tests/test_logic.py": "core", "tests/test_window.py": "ui"})
    monkeypatch.setattr(registry, "get_checks", lambda: [("one", None), ("two", None)])
    for suite in github.SUITES:
        write_json(tmp_path / suite / "result.json", {"suite": suite, "ok": True, "commands": [{"ok": True}]})
    write_json(tmp_path / "core/collected.json", ["tests/test_logic.py::test_first"])
    write_json(tmp_path / "ui/collected.json", ["tests/test_window.py::test_second"])
    write_json(tmp_path / "regression/regression.json", {
        "total": 2, "completed": 2, "failed": 0, "coverage_complete": True,
        "checks": [{"check": "one", "ok": True}, {"check": "two", "ok": True}],
    })
    return tmp_path


def test_gate_proof_requires_every_report_and_complete_collection(complete_reports):
    proof = github.build_proof(complete_reports, "abc")
    assert github.proof_matches(proof, "abc")
    assert proof["test_count"] == 2 and proof["regression_count"] == 2
    write_json(complete_reports / "ui/collected.json", [])
    with pytest.raises(ValueError):
        github.build_proof(complete_reports, "abc")


@pytest.mark.parametrize("change", [
    {"native_crash_retries": 1}, {"coverage_complete": False},
    {"completed": 1}, {"total": 1}, {"failed": 1},
    {"checks": [{"check": "one", "ok": True}, {"check": "one", "ok": True}]},
    {"checks": [{"check": "one", "ok": True}, {"check": "wrong", "ok": True}]},
    {"checks": [{"check": "one", "ok": True}, {"check": "two", "ok": False}]},
])
def test_gate_rejects_incomplete_or_retried_safety_results(complete_reports, change):
    path = complete_reports / "regression/regression.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    write_json(path, {**payload, **change})
    with pytest.raises(ValueError):
        github.build_proof(complete_reports, "abc")
