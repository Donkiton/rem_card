import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from scripts import build_release, publish_full_update


def test_publisher_rejects_local_windows_device_paths_as_network() -> None:
    assert not publish_full_update._is_network_path(
        Path(r"\\?\C:\Project\Baza_rao3_jurnal")
    )
    assert not publish_full_update._is_network_path(
        Path(r"\\.\C:\Project\Baza_rao3_jurnal")
    )
    assert publish_full_update._is_network_path(
        Path(r"\\server\share\Baza_rao3_jurnal")
    )
    assert publish_full_update._is_network_path(
        Path(r"\\?\UNC\server\share\Baza_rao3_jurnal")
    )


def _forbidden(name: str):
    def fail(*_args, **_kwargs):
        pytest.fail(f"{name} не должен вызываться в режиме --test-worktree")

    return fail


def _prepare_release_files(root: Path) -> dict[Path, bytes]:
    files = {
        root / "VERSION": b"3.15.0\n",
        root / "CHANGELOG.md": "# Изменения\n".encode("utf-8"),
        root / "app" / "release_info.json": b'{"version":"3.15.0"}\n',
    }
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return files


def _mock_successful_test_worktree(monkeypatch, root: Path) -> tuple[Path, list[str]]:
    package_dir = root / "dist" / "Prog"
    calls: list[str] = []

    monkeypatch.setattr(build_release, "project_root", lambda: root)
    monkeypatch.setattr(
        build_release,
        "ensure_git_repo",
        lambda checked_root: calls.append(f"git:{checked_root}"),
    )
    monkeypatch.setattr(build_release, "head_commit", lambda _root: "a" * 40)
    monkeypatch.setattr(
        build_release,
        "run_release_checks",
        lambda _root: calls.append("checks"),
    )

    def fake_run_build(_root: Path) -> Path:
        calls.append("build")
        (root / "build" / "temporary").mkdir(parents=True)
        package_dir.mkdir(parents=True)
        return package_dir

    monkeypatch.setattr(build_release, "run_build", fake_run_build)

    def fake_validate(package: Path, *, version: str, source_commit: str, **_kwargs):
        calls.append(f"validate:{version}:{source_commit}")
        assert package == package_dir
        return {}

    monkeypatch.setattr(build_release, "validate_full_package", fake_validate)
    monkeypatch.setattr(
        build_release,
        "run_compiled_smoke",
        lambda package: calls.append(f"smoke:{package}"),
    )

    for name in (
        "ensure_clean_tree",
        "update_release_files",
        "sync_current_release_info",
        "commit_release",
        "push_current_branch",
        "publish_built_release",
        "publish_local_release",
    ):
        monkeypatch.setattr(build_release, name, _forbidden(name))

    return package_dir, calls


@pytest.mark.parametrize(
    "argv",
    (
        ["auto", "--test-worktree"],
        ["patch", "--test-worktree"],
        ["--test-worktree", "--no-commit"],
        ["--test-worktree", "--skip-build"],
        ["--test-worktree", "--push"],
        ["--test-worktree", "--set", "4.0.0"],
        ["--test-worktree", "--change", "Тест"],
        ["--test-worktree", "--allow-empty"],
    ),
)
def test_test_worktree_rejects_release_arguments(argv: list[str]) -> None:
    args = build_release.parse_args(argv)

    with pytest.raises(SystemExit, match="--test-worktree нельзя сочетать"):
        build_release.validate_cli_args(args)


def test_test_worktree_accepts_default_auto_and_progress_json() -> None:
    args = build_release.parse_args(["--test-worktree", "--progress-json"])

    build_release.validate_cli_args(args)

    assert args.level == "auto"
    assert args.level_was_explicit is False


def test_test_worktree_keeps_metadata_and_dist_without_ready(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    release_files = _prepare_release_files(tmp_path)
    before = {path: path.read_bytes() for path in release_files}
    package_dir, calls = _mock_successful_test_worktree(monkeypatch, tmp_path)

    result = build_release.main(["--test-worktree", "--progress-json"])

    assert result == 0
    assert calls == [
        f"git:{tmp_path}",
        "checks",
        "build",
        f"validate:3.15.0:{'a' * 40}",
        f"smoke:{package_dir}",
    ]
    assert {path: path.read_bytes() for path in release_files} == before
    assert not (tmp_path / "build").exists()
    assert package_dir.is_dir()
    assert not (package_dir / build_release.READY_FILE_NAME).exists()

    marker = package_dir / build_release.TEST_WORKTREE_MARKER_NAME
    assert marker.is_file()
    marker_text = marker.read_text(encoding="utf-8")
    assert "НЕ ДЛЯ ПУБЛИКАЦИИ" in marker_text
    assert "незакоммиченные изменения" in marker_text
    assert "a" * 40 in marker_text

    output = capsys.readouterr().out
    assert f"Тестовая сборка рабочего дерева готова: {package_dir.resolve()}" in output
    progress_lines = [
        line.removeprefix(build_release.PROGRESS_JSON_PREFIX)
        for line in output.splitlines()
        if line.startswith(build_release.PROGRESS_JSON_PREFIX)
    ]
    events = [json.loads(line) for line in progress_lines]
    assert events
    assert all(event["schema_version"] == 1 for event in events)
    assert all(event["mode"] == "test_worktree" for event in events)
    assert all(
        set(event) >= {"schema_version", "mode", "stage", "status", "progress", "message"}
        for event in events
    )
    assert [event["progress"] for event in events] == sorted(
        event["progress"] for event in events
    )
    assert events[-1]["stage"] == "completed"
    assert events[-1]["status"] == "completed"
    assert events[-1]["progress"] == 100
    assert events[-1]["path"] == str(package_dir.resolve())


def test_failed_test_worktree_removes_build_and_dist(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _prepare_release_files(tmp_path)
    package_dir, _calls = _mock_successful_test_worktree(monkeypatch, tmp_path)

    def fail_smoke(_package: Path) -> None:
        raise RuntimeError("smoke не пройден")

    monkeypatch.setattr(build_release, "run_compiled_smoke", fail_smoke)

    with pytest.raises(RuntimeError, match="smoke не пройден"):
        build_release.main(["--test-worktree", "--progress-json"])

    assert not (tmp_path / "build").exists()
    assert not package_dir.exists()
    output = capsys.readouterr().out
    events = [
        json.loads(line.removeprefix(build_release.PROGRESS_JSON_PREFIX))
        for line in output.splitlines()
        if line.startswith(build_release.PROGRESS_JSON_PREFIX)
    ]
    assert events[-1]["stage"] == "failed"
    assert events[-1]["status"] == "failed"
    assert events[-1]["message"] == "smoke не пройден"
    assert events[-1]["progress"] == 72
    assert events[-1]["progress"] < 100
    cleanup_events = [event for event in events if event["stage"] == "cleanup"]
    assert cleanup_events
    assert {event["progress"] for event in cleanup_events} == {72}


def test_failed_checks_preserve_existing_dist_and_current_progress(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _prepare_release_files(tmp_path)
    existing_dist_file = tmp_path / "dist" / "Prog" / "previous-build.bin"
    existing_build_file = tmp_path / "build" / "previous-state.bin"
    existing_dist_file.parent.mkdir(parents=True)
    existing_build_file.parent.mkdir(parents=True)
    existing_dist_file.write_bytes(b"previous dist")
    existing_build_file.write_bytes(b"previous build")

    monkeypatch.setattr(build_release, "project_root", lambda: tmp_path)
    monkeypatch.setattr(build_release, "ensure_git_repo", lambda _root: None)
    monkeypatch.setattr(build_release, "head_commit", lambda _root: "d" * 40)

    def fail_checks(_root: Path) -> None:
        raise RuntimeError("обязательная проверка не пройдена")

    monkeypatch.setattr(build_release, "run_release_checks", fail_checks)
    monkeypatch.setattr(build_release, "run_build", _forbidden("run_build"))

    with pytest.raises(RuntimeError, match="обязательная проверка не пройдена"):
        build_release.main(["--test-worktree", "--progress-json"])

    assert existing_dist_file.read_bytes() == b"previous dist"
    assert existing_build_file.read_bytes() == b"previous build"
    output = capsys.readouterr().out
    events = [
        json.loads(line.removeprefix(build_release.PROGRESS_JSON_PREFIX))
        for line in output.splitlines()
        if line.startswith(build_release.PROGRESS_JSON_PREFIX)
    ]
    assert not any(event["stage"] == "cleanup" for event in events)
    assert events[-1]["stage"] == "failed"
    assert events[-1]["progress"] == 5
    assert all(event["progress"] < 100 for event in events)


def test_test_marker_rejects_ready_ok(tmp_path: Path) -> None:
    package_dir = tmp_path / "dist" / "Prog"
    package_dir.mkdir(parents=True)
    (package_dir / build_release.READY_FILE_NAME).write_text("ready", encoding="utf-8")

    with pytest.raises(RuntimeError, match="не может содержать ready.ok"):
        build_release.write_test_worktree_marker(
            package_dir,
            source_commit="b" * 40,
        )

    assert not (package_dir / build_release.TEST_WORKTREE_MARKER_NAME).exists()


def test_local_release_publisher_rejects_test_worktree_marker(tmp_path: Path) -> None:
    package_dir = tmp_path / "dist" / "Prog"
    package_dir.mkdir(parents=True)
    marker = package_dir / build_release.TEST_WORKTREE_MARKER_NAME
    marker.write_text("test only", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Публикация тестовой сборки запрещена") as exc_info:
        build_release.publish_local_release(
            tmp_path,
            package_dir,
            version="3.15.0",
            source_commit="e" * 40,
        )

    assert str(marker) in str(exc_info.value)


def test_production_publisher_rejects_test_worktree_marker(tmp_path: Path) -> None:
    source = tmp_path / "3.15.0"
    source.mkdir()
    marker = source / publish_full_update.TEST_WORKTREE_MARKER_NAME
    marker.write_text("test only", encoding="utf-8")
    (source / publish_full_update.READY_FILE_NAME).write_text("ready", encoding="utf-8")

    with pytest.raises(
        publish_full_update.PublishError,
        match="Production-публикация тестовой сборки запрещена",
    ) as exc_info:
        publish_full_update._validate_release(source)

    assert str(marker) in str(exc_info.value)


def test_regular_release_emits_progress_for_all_pipeline_stages(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    args = build_release.parse_args(["--progress-json"])
    package_dir = tmp_path / "dist" / "Prog"
    published_dir = tmp_path / "UPD" / "releases" / "3.15.0"
    source_commit = "c" * 40

    monkeypatch.setattr(
        build_release,
        "has_staged_or_unstaged_release_file_changes",
        lambda _root: False,
    )
    monkeypatch.setattr(build_release, "ensure_clean_tree", lambda _root: None)
    monkeypatch.setattr(build_release, "head_commit", lambda _root: source_commit)
    monkeypatch.setattr(build_release, "run_release_checks", lambda _root: None)
    monkeypatch.setattr(build_release, "run_build", lambda _root: package_dir)
    monkeypatch.setattr(build_release, "validate_full_package", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(build_release, "run_compiled_smoke", lambda _package: None)
    monkeypatch.setattr(build_release, "push_current_branch", lambda _root: source_commit)
    monkeypatch.setattr(
        build_release,
        "publish_built_release",
        lambda *_args, **_kwargs: published_dir,
    )
    monkeypatch.setattr(build_release, "cleanup_build_artifacts", lambda *_args, **_kwargs: None)

    build_release.finish_release(tmp_path, "3.15.0", args)

    output = capsys.readouterr().out
    events = [
        json.loads(line.removeprefix(build_release.PROGRESS_JSON_PREFIX))
        for line in output.splitlines()
        if line.startswith(build_release.PROGRESS_JSON_PREFIX)
    ]
    started_stages = [
        event["stage"] for event in events if event["status"] == "started"
    ]
    assert started_stages == [
        "checks",
        "build",
        "validate",
        "smoke",
        "push",
        "publish",
        "cleanup",
    ]
    assert all(event["mode"] == "release" for event in events)
    publish_completed = next(
        event
        for event in events
        if event["stage"] == "publish" and event["status"] == "completed"
    )
    assert publish_completed["path"] == str(published_dir.resolve())
