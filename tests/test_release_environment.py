from importlib import metadata
from types import SimpleNamespace

import pytest

from scripts import release_environment as environment


def test_mismatched_installed_version_rejects_release(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("pandas==3.1.0\n", encoding="utf-8")
    monkeypatch.setattr(metadata, "version", lambda _name: "3.0.6")
    with pytest.raises(RuntimeError, match="pandas: установлен 3.0.6, требуется 3.1.0"):
        environment.validate_build_environment(tmp_path)


def test_active_markers_and_missing_dependencies_are_checked(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text(
        'ignored==1.0; python_version < "2"\nmissing==1.0\n', encoding="utf-8",
    )
    calls = []

    def missing(name):
        calls.append(name)
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "version", missing)
    with pytest.raises(RuntimeError, match="missing: не установлен"):
        environment.validate_build_environment(tmp_path)
    assert calls == ["missing"]


def test_manifest_environment_records_actual_exact_versions(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("# runtime\nPandas==3.1.0\n", encoding="utf-8")
    monkeypatch.setattr(metadata, "version", lambda _name: "3.1.0")
    report = environment.validate_build_environment(tmp_path)
    assert report["packages"] == {"pandas": "3.1.0"}
    assert len(report["requirements_sha256"]) == 64


@pytest.mark.parametrize("requirement", ["pandas>=3.0", "pandas==3.*"])
def test_unpinned_requirements_are_rejected(tmp_path, requirement):
    (tmp_path / "requirements.txt").write_text(requirement + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="точную версию"):
        environment.validate_build_environment(tmp_path)


def test_bundled_versions_are_inspected_without_executing_modules(tmp_path, monkeypatch):
    from PyInstaller.archive import readers

    sources = {
        "pandas._version_meson": '__version__="3.0.6"\nraise RuntimeError("must not execute")',
        "numpy.version": 'version="2.4.6"',
        "PySide6": '__version__="6.12.0"',
    }
    archive = SimpleNamespace(extract=lambda name: compile(sources[name], name, "exec"))
    monkeypatch.setattr(readers, "CArchiveReader", lambda _path: SimpleNamespace(
        open_embedded_archive=lambda name: archive,
    ))
    expected = {"pandas": "3.1.0", "numpy": "2.4.6", "pyside6": "6.12.0"}
    with pytest.raises(RuntimeError, match="упакован pandas 3.0.6"):
        environment.verify_bundled_runtime_versions(tmp_path / "RemCard.exe", expected)
    expected["pandas"] = "3.0.6"
    assert environment.verify_bundled_runtime_versions(tmp_path / "RemCard.exe", expected) == expected


def test_release_build_stops_before_compilation_on_environment_mismatch(tmp_path, monkeypatch):
    from scripts import build_release

    def reject(_root):
        raise RuntimeError("dependency mismatch")

    def forbidden(*_args, **_kwargs):
        pytest.fail("compiler or cleanup started before environment validation")

    monkeypatch.setattr(build_release, "validate_build_environment", reject)
    monkeypatch.setattr(build_release, "cleanup_build_artifacts", forbidden)
    monkeypatch.setattr(build_release, "run", forbidden)
    with pytest.raises(RuntimeError, match="dependency mismatch"):
        build_release.run_build(tmp_path)


def test_release_manifest_records_verified_bundled_versions(tmp_path, monkeypatch):
    import json
    from scripts import build_release

    package = tmp_path / "dist" / "Prog"
    expected = {"packages": {"pandas": "3.1.0"}}
    monkeypatch.setattr(build_release, "validate_build_environment", lambda _root: dict(expected))
    monkeypatch.setattr(build_release, "cleanup_build_artifacts", lambda _root: None)
    monkeypatch.setattr(build_release, "get_dev_baza_dir", lambda: str(tmp_path / "synthetic"))
    monkeypatch.setattr(build_release, "verify_bundled_runtime_versions", lambda exe, packages: (
        {"pandas": "3.1.0"} if exe == package / "RemCard.exe" and packages == expected["packages"] else None
    ))

    def compile_fixture(*_args, **_kwargs):
        package.mkdir(parents=True)
        (package / "manifest.json").write_text('{"version":"5.1.17"}', encoding="utf-8")

    monkeypatch.setattr(build_release, "run", compile_fixture)
    assert build_release.run_build(tmp_path) == package
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["build_environment"] == {
        **expected, "bundled_runtime_versions": {"pandas": "3.1.0"},
    }
