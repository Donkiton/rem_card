from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
SCRIPTS_DIR = PROJECT_DIR / "scripts"
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from rem_card.app import update_checker, update_launcher, updater_main  # noqa: E402
from rem_card.app.update_checker import UpdateCandidate  # noqa: E402
from rem_card.app.full_update_manifest import (  # noqa: E402
    FullUpdateManifestError,
    build_file_inventory,
)
from scripts import build_release, publish_full_update  # noqa: E402


def _write_update_package(root: Path, executable_names: tuple[str, ...]) -> None:
    root.mkdir(parents=True)
    (root / "ready.ok").write_text("ready\n", encoding="utf-8")
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "app": "rem_card",
                "package_type": "full",
                "version": "4.4.0",
                "prog_dir": ".",
            }
        ),
        encoding="utf-8",
    )
    (root / "_internal").mkdir()
    for name in executable_names:
        (root / name).write_bytes(name.encode("ascii"))


@pytest.mark.parametrize(
    ("required_exes", "expected_layout"),
    (
        (updater_main.UNIFIED_REQUIRED_EXES, "unified"),
        (updater_main.LEGACY_REQUIRED_EXES, "legacy"),
    ),
)
def test_updater_accepts_unified_and_legacy_full_packages(
    tmp_path: Path,
    required_exes: tuple[str, ...],
    expected_layout: str,
) -> None:
    package = tmp_path / expected_layout
    _write_update_package(package, required_exes)

    manifest = updater_main._validate_source(str(package))

    assert manifest["version"] == "4.4.0"
    assert updater_main._detect_package_layout(str(package)) == expected_layout


def test_updater_rejects_partial_mixed_executable_layout(tmp_path: Path) -> None:
    package = tmp_path / "partial"
    _write_update_package(
        package,
        ("RemCardUpdater.exe", "RemCardDoctor.exe", "RemCardNurse.exe"),
    )

    with pytest.raises(RuntimeError, match="ни единой, ни прежней структуре"):
        updater_main._validate_source(str(package))


def test_unified_layout_does_not_bypass_manifest_hash_verification(tmp_path: Path) -> None:
    source = tmp_path / "release"
    target = tmp_path / "install"
    _write_update_package(source, updater_main.UNIFIED_REQUIRED_EXES)
    target.mkdir()
    installed = target / "RemCard.exe"
    installed.write_bytes(b"installed")

    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    manifest["schema_version"] = 2
    manifest["files"] = build_file_inventory(source)
    (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (source / "RemCard.exe").write_bytes(b"tampered123")

    with pytest.raises(FullUpdateManifestError, match="SHA-256"):
        updater_main._replace_program_dir(
            source_dir=str(source),
            target_dir=str(target),
            status=lambda *_args: None,
            expected_manifest=manifest,
        )

    assert installed.read_bytes() == b"installed"


@pytest.mark.parametrize(
    "required_exes",
    (update_checker.UNIFIED_REQUIRED_RELEASE_EXES, update_checker.LEGACY_REQUIRED_RELEASE_EXES),
)
def test_update_checker_discovers_both_supported_layouts(
    tmp_path: Path,
    required_exes: tuple[str, ...],
) -> None:
    package = tmp_path / "4.4.0"
    _write_update_package(package, required_exes)

    candidate = update_checker._load_candidate(str(package))

    assert candidate is not None
    assert candidate.version == "4.4.0"


def test_process_detection_includes_unified_and_legacy_targets(
    monkeypatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "install"
    other = tmp_path / "other"
    target.mkdir()
    other.mkdir()
    monkeypatch.setattr(
        updater_main,
        "_iter_running_processes",
        lambda: [
            (101, "RemCard.exe", str(target / "RemCard.exe")),
            (102, "RemCardDoctor.exe", str(target / "RemCardDoctor.exe")),
            (103, "RemCard.exe", str(other / "RemCard.exe")),
        ],
    )

    assert updater_main._find_running_target_processes(str(target)) == [
        (101, "RemCard.exe"),
        (102, "RemCardDoctor.exe"),
    ]


def test_legacy_restart_falls_back_to_unified_entry(tmp_path: Path) -> None:
    target = tmp_path / "install"
    target.mkdir()
    unified = target / "RemCard.exe"
    unified.write_bytes(b"new")

    resolved = updater_main._resolve_restart_path(
        str(target),
        str(target / "RemCardDoctor.exe"),
    )

    assert resolved == str(unified)


def test_existing_legacy_restart_target_is_preserved(tmp_path: Path) -> None:
    target = tmp_path / "install"
    target.mkdir()
    legacy = target / "RemCardNurse.exe"
    legacy.write_bytes(b"old")

    assert updater_main._resolve_restart_path(str(target), str(legacy)) == str(legacy)


def test_unified_launcher_requests_remcard_restart(monkeypatch, tmp_path: Path) -> None:
    candidate = UpdateCandidate(
        version="4.4.0",
        release_dir=str(tmp_path),
        prog_dir=str(tmp_path),
        manifest_path=str(tmp_path / "manifest.json"),
        manifest={"version": "4.4.0"},
    )
    calls: list[tuple[object, str | None, bool]] = []

    def fake_launch(item, *, restart_exe=None, wait_for_parent=True):
        calls.append((item, restart_exe, wait_for_parent))
        return True

    monkeypatch.setattr(update_launcher, "launch_update", fake_launch)

    assert update_launcher.launch_unified_update(candidate) is True
    assert calls == [(candidate, "RemCard.exe", True)]


def test_build_validation_requires_exact_two_executable_layout(
    monkeypatch,
    tmp_path: Path,
) -> None:
    package = tmp_path / "Prog"
    package.mkdir()
    (package / "_internal").mkdir()
    for name in build_release.REQUIRED_RELEASE_EXES:
        (package / name).write_bytes(b"exe")
    manifest = {
        "app": "rem_card",
        "package_type": "full",
        "version": "4.4.0",
        "prog_dir": ".",
        "source_commit": "a" * 40,
    }
    (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(build_release, "validate_settings_release_snapshot", lambda *_a, **_kw: {})

    assert build_release.validate_full_package(
        package,
        version="4.4.0",
        source_commit="a" * 40,
    ) == manifest

    (package / "RemCardDoctor.exe").write_bytes(b"legacy")
    with pytest.raises(RuntimeError, match="найдены лишние EXE"):
        build_release.validate_full_package(
            package,
            version="4.4.0",
            source_commit="a" * 40,
        )


@pytest.mark.parametrize(
    ("required_exes", "expected_layout"),
    (
        (publish_full_update.UNIFIED_REQUIRED_RELEASE_EXES, "unified"),
        (publish_full_update.LEGACY_REQUIRED_RELEASE_EXES, "legacy"),
    ),
)
def test_network_publisher_recognizes_both_layouts(
    tmp_path: Path,
    required_exes: tuple[str, ...],
    expected_layout: str,
) -> None:
    package = tmp_path / expected_layout
    package.mkdir()
    for name in required_exes:
        (package / name).write_bytes(b"exe")

    assert publish_full_update._detect_executable_layout(package) == expected_layout


def test_network_publisher_rejects_mixed_final_layout(tmp_path: Path) -> None:
    package = tmp_path / "mixed"
    package.mkdir()
    for name in (
        *publish_full_update.UNIFIED_REQUIRED_RELEASE_EXES,
        "RemCardDoctor.exe",
    ):
        (package / name).write_bytes(b"exe")

    with pytest.raises(publish_full_update.PublishError, match="EXE прежних отдельных ролей"):
        publish_full_update._detect_executable_layout(package)
