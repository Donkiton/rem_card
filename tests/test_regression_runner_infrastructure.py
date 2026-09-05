from __future__ import annotations

import ast
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

import pytest

from scripts import regression_safety_checks as regression
from scripts import sanity_failfast_runner as sanity
from scripts.regression_checks.scheduling import partition_checks


def _child_report_command(check_names: list[str], *, exit_code: int = 0) -> list[str]:
    checks = [
        {"check": name, "ok": True, "details": "ok", "duration_sec": 0.001}
        for name in check_names
    ]
    payload = {
        "total": len(checks),
        "failed": 0,
        "passed": len(checks),
        "completed": len(checks),
        "coverage_complete": True,
        "checks": checks,
    }
    code = (
        "import json, sys; "
        f"print(json.dumps({payload!r}, ensure_ascii=False, indent=2)); "
        f"sys.exit({int(exit_code)})"
    )
    return [sys.executable, "-c", code]


def test_cached_source_segment_matches_stdlib_for_unicode_and_multiline():
    source = 'def probe():\n    текст = (\n        "да"\n    )\n    return текст\n'
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if getattr(node, "end_lineno", None) is not None:
            assert regression._cached_source_segment(source, node) == ast.get_source_segment(source, node)


def test_fast_profile_is_default_and_exhaustive_remains_available():
    defaults = regression._parse_args([])
    exhaustive = regression._parse_args(["--profile", "exhaustive", "--json-detail", "all"])

    assert defaults.profile == "fast"
    assert defaults.json_detail == "summary"
    assert defaults.timeout_s == 600.0
    assert exhaustive.profile == "exhaustive"
    assert exhaustive.json_detail == "all"


def test_worker_shards_cover_registry_once_without_overlap():
    checks = [(f"check_{index}", object()) for index in range(37)]
    shards = [
        regression._select_worker_shard(checks, shard_index=index, shard_count=4)
        for index in range(4)
    ]
    names = [name for shard in shards for name, _fn in shard]

    assert sorted(names) == sorted(name for name, _fn in checks)
    assert len(names) == len(set(names))
    assert [name for name, _fn in shards[0]] == [f"check_{index}" for index in range(9)]
    with pytest.raises(ValueError):
        regression._select_worker_shard(checks, shard_index=4, shard_count=4)


def test_duration_partition_preserves_order_and_improves_uneven_workload():
    checks = [(str(index), object()) for index in range(12)]
    estimates = {str(index): 1.0 if index < 9 else 12.0 for index in range(12)}
    groups = partition_checks(checks, 4, estimates)
    assert [item for group in groups for item in group] == checks
    assert len(groups) == 4 and all(groups)
    costs = [sum(estimates[name] for name, _ in group) for group in groups]
    assert max(costs) == 12.0  # Равное деление по количеству давало 36 секунд.


def test_duration_partition_is_optimal_for_small_exhaustive_cases():
    from itertools import combinations, product

    for weights in product((1, 3), repeat=5):
        checks = [(str(i), object()) for i in range(5)]
        estimates = {str(i): value for i, value in enumerate(weights)}
        for count in range(1, 6):
            groups = partition_checks(checks, count, estimates)
            actual = max(sum(estimates[name] for name, _ in group) for group in groups)
            alternatives = []
            for cuts in combinations(range(1, 5), count - 1):
                points = (0, *cuts, 5)
                alternatives.append(max(sum(weights[a:b]) for a, b in zip(points, points[1:])))
            assert actual == min(alternatives)
            assert [item for group in groups for item in group] == checks


def test_duration_partition_includes_new_unmeasured_checks():
    checks = [("known", object()), ("new", object()), ("another", object())]
    groups = partition_checks(checks, 2, {"known": 4.0})
    assert [item for group in groups for item in group] == checks


def test_worker_exit_one_with_complete_payload_is_a_test_failure_not_infrastructure_crash():
    assert regression._worker_report_is_structurally_valid(
        {"exit_code": 1, "payload": {"total": 1, "failed": 1, "checks": []}}
    )
    assert not regression._worker_report_is_structurally_valid(
        {
            "exit_code": -1073741819,
            "payload": None,
            "error": "worker exit=-1073741819; stderr_tail='access violation'",
        }
    )
    assert regression._looks_like_native_worker_crash(-1073741819)
    assert regression._looks_like_native_worker_crash(3221225477)
    assert regression._looks_like_native_worker_crash(2, "Fatal Python error: access violation")
    assert not regression._looks_like_native_worker_crash(23)


def test_real_child_timeout_and_abnormal_exit_are_classified():
    timeout_worker = regression._run_isolated_worker_command(
        command=[sys.executable, "-c", "import time; time.sleep(5)"],
        shard_index=0,
        deadline_monotonic=time.monotonic() + 0.2,
        env=os.environ.copy(),
    )
    assert timeout_worker["timed_out"] is True
    assert timeout_worker["crashed"] is False
    assert timeout_worker["failure_kind"] == "timeout"
    assert not regression._worker_report_is_structurally_valid(timeout_worker)

    crashed_worker = regression._run_isolated_worker_command(
        command=[sys.executable, "-c", "import os; os._exit(23)"],
        shard_index=1,
        deadline_monotonic=time.monotonic() + 2.0,
        env=os.environ.copy(),
    )
    assert crashed_worker["timed_out"] is False
    assert crashed_worker["crashed"] is True
    assert crashed_worker["native_crash"] is False
    assert crashed_worker["failure_kind"] == "abnormal_exit"
    assert crashed_worker["exit_code"] == 23
    assert not regression._worker_report_is_structurally_valid(crashed_worker)


def test_jobs_one_merges_real_isolated_shards_as_fast_profile(monkeypatch, capsys):
    checks = [(f"check_{index}", object()) for index in range(4)]
    calls: list[int] = []

    def run_real_child(*, shard_index, shard_count, deadline_monotonic, temp_root):
        _ = temp_root
        calls.append(shard_index)
        names = [
            name
            for name, _fn in regression._select_worker_shard(
                checks,
                shard_index=shard_index,
                shard_count=shard_count,
            )
        ]
        return regression._run_isolated_worker_command(
            command=_child_report_command(names),
            shard_index=shard_index,
            deadline_monotonic=deadline_monotonic,
            env=os.environ.copy(),
        )

    monkeypatch.setattr(regression, "_run_worker_process", run_real_child)
    with pytest.raises(SystemExit) as raised:
        regression._run_parallel_profile(
            checks,
            jobs=1,
            shard_count=2,
            timeout_s=5.0,
            temp_root="",
            quiet=True,
            json_detail="all",
            report_path="",
            deadline_monotonic=time.monotonic() + 5.0,
        )

    assert raised.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["profile"] == "fast"
    assert payload["jobs"] == 1
    assert payload["shards"] == 2
    assert payload["completed"] == payload["passed"] == payload["total"] == 4
    assert payload["coverage_complete"] is True
    assert payload["timed_out"] is False
    assert payload["worker_crash"] is False
    assert payload["native_crash"] is False
    assert sorted(calls) == [0, 1]


def test_fast_global_deadline_covers_queued_shards(monkeypatch, tmp_path, capsys):
    checks = [("first", object()), ("second", object())]
    markers = [tmp_path / f"shard_{index}.started" for index in range(2)]

    def run_sleeping_child(*, shard_index, shard_count, deadline_monotonic, temp_root):
        _ = (shard_count, temp_root)
        code = (
            "from pathlib import Path; import time; "
            f"Path({str(markers[shard_index])!r}).write_text('started', encoding='utf-8'); "
            "time.sleep(5)"
        )
        return regression._run_isolated_worker_command(
            command=[sys.executable, "-c", code],
            shard_index=shard_index,
            deadline_monotonic=deadline_monotonic,
            env=os.environ.copy(),
        )

    monkeypatch.setattr(regression, "_run_worker_process", run_sleeping_child)
    deadline = time.monotonic() + 0.75
    with pytest.raises(SystemExit) as raised:
        regression._run_parallel_profile(
            checks,
            jobs=1,
            shard_count=2,
            timeout_s=0.75,
            temp_root="",
            quiet=True,
            json_detail="all",
            report_path="",
            deadline_monotonic=deadline,
        )

    assert raised.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    # Expensive tail shards are scheduled first. The queued shard must not start
    # after the one shared deadline has already expired.
    assert markers[1].exists()
    assert not markers[0].exists()
    assert payload["timed_out"] is True
    assert payload["worker_crash"] is False
    assert payload["native_crash"] is False
    assert payload["coverage_complete"] is False
    assert all(worker["timed_out"] for worker in payload["workers"])
    assert all(worker["failure_kind"] == "timeout" for worker in payload["workers"])


def test_real_child_crash_is_reported_by_parallel_merge(monkeypatch, capsys):
    checks = [("native_sensitive", object())]

    def run_crashing_child(*, shard_index, shard_count, deadline_monotonic, temp_root):
        _ = (shard_count, temp_root)
        return regression._run_isolated_worker_command(
            command=[sys.executable, "-c", "import os; os._exit(37)"],
            shard_index=shard_index,
            deadline_monotonic=deadline_monotonic,
            env=os.environ.copy(),
        )

    monkeypatch.setattr(regression, "_run_worker_process", run_crashing_child)
    with pytest.raises(SystemExit) as raised:
        regression._run_parallel_profile(
            checks,
            jobs=1,
            shard_count=1,
            timeout_s=2.0,
            temp_root="",
            quiet=True,
            json_detail="all",
            report_path="",
            deadline_monotonic=time.monotonic() + 2.0,
        )

    assert raised.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["worker_crash"] is True
    assert payload["native_crash"] is False
    assert payload["timed_out"] is False
    assert payload["coverage_complete"] is False
    assert payload["workers"][0]["failure_kind"] == "abnormal_exit"
    assert payload["workers"][0]["exit_code"] == 37


def test_native_crash_worker_is_retried_once_in_isolation(monkeypatch):
    worker_meta = [
        {
            "shard_index": 2,
            "exit_code": 3221225477,
            "error": "worker native_crash=3221225477",
            "native_crash": True,
            "payload": None,
        },
        {
            "shard_index": 3,
            "exit_code": 23,
            "error": "worker exit=23",
            "native_crash": False,
            "payload": None,
        },
    ]
    calls: list[int] = []

    def run_recovered_worker(*, shard_index, shard_count, deadline_monotonic, temp_root):
        _ = (shard_count, deadline_monotonic, temp_root)
        calls.append(shard_index)
        return {
            "shard_index": shard_index,
            "exit_code": 0,
            "error": "",
            "native_crash": False,
            "crashed": False,
            "timed_out": False,
            "payload": {
                "checks": [
                    {"check": "recovered", "ok": True, "details": "ok", "duration_sec": 0.1}
                ]
            },
        }

    monkeypatch.setattr(regression, "_run_worker_process", run_recovered_worker)

    retries = regression._retry_native_crash_workers(
        worker_meta,
        shard_count=16,
        deadline_monotonic=time.monotonic() + 5.0,
        temp_root="",
        quiet=True,
    )

    assert retries == 1
    assert calls == [2]
    assert worker_meta[0]["retried_native_crash"] is True
    assert worker_meta[0]["initial_exit_code"] == 3221225477
    assert regression._worker_report_is_structurally_valid(worker_meta[0])
    assert worker_meta[1]["exit_code"] == 23
    assert [item["check"] for item in regression._parallel_results_from_workers(worker_meta)] == [
        "recovered"
    ]


def test_main_routes_fast_jobs_one_through_parallel_subprocess_path(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    def stop_after_routing(checks, **kwargs):
        captured["total"] = len(checks)
        captured.update(kwargs)
        raise SystemExit(0)

    monkeypatch.setattr(regression, "_cleanup_orphan_direct_temp_roots", lambda: None)
    monkeypatch.setattr(regression, "_make_temp_root", lambda: str(tmp_path))
    monkeypatch.setattr(regression, "_prepare_import_environment", lambda _root: None)
    monkeypatch.setattr(regression, "_rmtree_regression_root", lambda _root: "")
    monkeypatch.setattr(regression, "_run_parallel_profile", stop_after_routing)
    monkeypatch.setattr(regression.faulthandler, "enable", lambda: None)
    monkeypatch.setattr(regression.faulthandler, "dump_traceback_later", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(regression.faulthandler, "cancel_dump_traceback_later", lambda: None)

    with pytest.raises(SystemExit) as raised:
        regression.main(
            [
                "--profile",
                "fast",
                "--jobs",
                "1",
                "--shards",
                "2",
                "--timeout-s",
                "0.01",
                "--quiet-progress",
            ]
        )

    assert raised.value.code == 0
    assert captured["total"] == 538
    assert captured["jobs"] == 1
    assert captured["shard_count"] == 2
    assert captured["deadline_monotonic"] is not None


def test_compiled_path_probe_preserves_non_ascii_temp_path(tmp_path):
    probe_root = tmp_path / "проверка_пути"
    probe_root.mkdir()

    ok, details = regression._check_arbitrary_baza_dir_name_allowed(str(probe_root))

    assert ok, details


def test_summary_stdout_is_compact_and_optional_report_keeps_all_checks(tmp_path, capsys):
    report_path = tmp_path / "full.json"
    report = {
        "total": 3,
        "failed": 1,
        "passed": 2,
        "completed": 3,
        "coverage_complete": True,
        "checks": [
            {"check": "ok_fast", "ok": True, "duration_sec": 0.1, "details": "ok"},
            {"check": "failed", "ok": False, "duration_sec": 0.2, "details": "boom"},
            {"check": "ok_slow", "ok": True, "duration_sec": 4.0, "details": "ok"},
        ],
    }

    regression._emit_regression_report(
        report,
        json_detail="summary",
        report_path=str(report_path),
    )

    stdout_payload = json.loads(capsys.readouterr().out)
    disk_payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert [item["check"] for item in stdout_payload["checks"]] == ["failed"]
    assert stdout_payload["slowest_checks"][0]["check"] == "ok_slow"
    assert len(stdout_payload["check_manifest_sha256"]) == 64
    assert len(disk_payload["checks"]) == 3


def test_runner_cleanup_releases_every_registered_probe():
    released: list[str] = []

    class Probe:
        def __init__(self, name: str):
            self.name = name

        def release_network_emergency_role_marker(self):
            released.append(self.name)

    regression._REGRESSION_RESTORE_PROBES.extend((Probe("first"), Probe("second")))
    try:
        assert regression._cleanup_check_resources() == []
    finally:
        regression._REGRESSION_RESTORE_PROBES.clear()

    assert released == ["second", "first"]


def test_managed_temp_cleanup_removes_readonly_tree():
    root = Path(tempfile.mkdtemp(prefix="remcard_regression_checks_unit_"))
    readonly_file = root / "nested" / ".git" / "objects" / "readonly"
    readonly_file.parent.mkdir(parents=True)
    readonly_file.write_text("fixture", encoding="utf-8")
    os.chmod(readonly_file, stat.S_IREAD)

    error = regression._rmtree_regression_root(root)

    assert error == ""
    assert not root.exists()


def test_sanity_json_parser_and_validator_accept_complete_compact_report():
    payload = {
        "total": 556,
        "failed": 0,
        "passed": 556,
        "completed": 556,
        "coverage_complete": True,
        "checks": [],
    }
    output = "log with {noise}\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    parsed = sanity._extract_last_json_dict(output)
    assert parsed == payload
    assert sanity._validate_regression(0, parsed) == (True, "Passed 556/556")

    incomplete = dict(payload, completed=555, coverage_complete=False)
    ok, reason = sanity._validate_regression(0, incomplete)
    assert not ok
    assert "completed=555" in reason
