from __future__ import annotations

import json
from pathlib import Path

import pytest

from rem_card.app.isolated_test_runtime import PURPOSE, sandbox_paths, test_share_name as share_name


def marker(root: Path) -> None:
    (root / "TEST_SANDBOX.json").write_text(
        json.dumps({"schema_version": 1, "purpose": PURPOSE}), encoding="utf-8"
    )


def test_profile_missing_marker_fails_closed(tmp_path):
    with pytest.raises(FileNotFoundError):
        sandbox_paths(tmp_path)


def test_profile_invalid_marker_fails_closed(tmp_path):
    (tmp_path / "TEST_SANDBOX.json").write_text('{"schema_version": 1}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="маркер"):
        sandbox_paths(tmp_path)


def test_profile_rejects_data_redirect_outside_bundle(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    marker(bundle)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (bundle / "State").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlinks unavailable: {exc}")
    with pytest.raises(RuntimeError, match="пределы"):
        sandbox_paths(bundle)


def test_profile_has_only_bundle_owned_paths_and_separate_share(tmp_path):
    marker(tmp_path)
    paths = sandbox_paths(tmp_path)
    assert all(Path(path).is_relative_to(tmp_path) for path in paths.values())
    assert share_name(tmp_path).startswith("RemCardTest_")
    assert share_name(tmp_path) != share_name(tmp_path / "another")


def test_test_namespace_keeps_installed_role_separate(monkeypatch):
    from rem_card.app import main

    monkeypatch.setattr(main, "is_compiled", lambda: True)
    monkeypatch.delenv("REMCARD_TEST_INSTANCE_NAMESPACE", raising=False)
    installed = main._single_instance_server_name("nurse")
    monkeypatch.setenv("REMCARD_TEST_INSTANCE_NAMESPACE", "C:\\RemCard Test")
    test = main._single_instance_server_name("nurse")
    assert installed == "rem_card_single_instance_server_nurse"
    assert test != installed
    assert main._single_instance_server_name("doctor") != test
    monkeypatch.setenv("REMCARD_TEST_INSTANCE_NAMESPACE", "C:\\Another Test")
    assert main._single_instance_server_name("nurse") != test


def test_builder_excludes_foreign_native_dll_paths(monkeypatch, tmp_path):
    from scripts import build_emergency_test_bundle as builder

    monkeypatch.setenv("PATH", r"C:\ForeignTool\poppler\bin")
    monkeypatch.setenv("REMCARD_BAZA_DIR", r"C:\DoNotUseThisDatabase")
    monkeypatch.setenv("REMCARD_EMERGENCY_DB_ROOT", r"C:\DoNotUseThisEmergency")
    monkeypatch.setattr(builder.shutil, "which", lambda *args, **kwargs: r"C:\Program Files\Git\cmd\git.exe")
    env = builder.prepare_environment(tmp_path)
    assert "ForeignTool" not in env["PATH"]
    assert "System32" in env["PATH"]
    assert env["REMCARD_BAZA_DIR"] == str(tmp_path / "seed")
    assert "REMCARD_EMERGENCY_DB_ROOT" not in env
    assert Path(env["ProgramData"]).is_relative_to(tmp_path)
    assert Path(env["USERPROFILE"]).is_relative_to(tmp_path)
    assert env["HOME"] == env["USERPROFILE"]


def test_organization_qsettings_overload_uses_sandbox_file(monkeypatch, tmp_path):
    from PySide6 import QtCore
    from rem_card.app.isolated_test_runtime import isolate_qsettings

    original = QtCore.QSettings
    monkeypatch.setattr(QtCore, "QSettings", original)
    settings_class = isolate_qsettings(str(tmp_path))
    settings = settings_class("MyHospital", "RemCard")
    assert settings.format() == original.IniFormat
    assert Path(settings.fileName()).is_relative_to(tmp_path)
    settings.setValue("sandbox_test", "isolated")
    settings.sync()
    assert Path(settings.fileName()).is_file()
    from PySide6.QtCore import QSettings

    assert QSettings("MyHospital", "RemCard").value("sandbox_test") == "isolated"


def test_home_based_analytics_preferences_are_isolated(monkeypatch, tmp_path):
    from rem_card.app.isolated_test_runtime import user_profile_environment
    from rem_card.services.analytics.platform.core import SavedAnalyticsViewStore

    profile = tmp_path / "State" / "UserProfile"
    for key, value in user_profile_environment(profile).items():
        monkeypatch.setenv(key, value)
    assert Path.home() == profile
    store = SavedAnalyticsViewStore()
    assert store.path == profile / ".remcard_analytics_views.json"
    store.save([])
    assert store.path.read_text(encoding="utf-8") == "[]"
    assert store.load() == ()


def test_bundle_zip_preserves_empty_database_directories_after_extraction(tmp_path):
    import zipfile
    from scripts.build_emergency_test_bundle import package_bundle

    source = tmp_path / "source"
    source.mkdir()
    marker(source)
    for directory in ("archiv/db_cycle_archive", "quarantine/shared_db", "settings/backups"):
        (source / "Database" / directory).mkdir(parents=True)
    (source / "Database" / "archiv" / "rao_journal.db").write_bytes(b"synthetic fixture")
    output = package_bundle(source, tmp_path / "test.zip")
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(output) as archive:
        archive.extractall(extracted)
    restored = extracted / "RemCard-Emergency-Test"
    for path in source.rglob("*"):
        target = restored / path.relative_to(source)
        if path.is_dir():
            assert target.is_dir()
        else:
            assert target.read_bytes() == path.read_bytes()


def test_bundle_zip_refuses_test_state_and_overwriting(tmp_path):
    from scripts.build_emergency_test_bundle import package_bundle

    marker(tmp_path)
    output = tmp_path.parent / (tmp_path.name + ".zip")
    (tmp_path / "State").mkdir()
    with pytest.raises(RuntimeError, match="состояния"):
        package_bundle(tmp_path, output)
    (tmp_path / "State").rmdir()
    output.write_bytes(b"existing user archive")
    with pytest.raises(FileExistsError):
        package_bundle(tmp_path, output)
    assert output.read_bytes() == b"existing user archive"
