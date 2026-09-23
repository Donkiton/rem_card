from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


def _successful_result(validation, path: str, *, schema_version: int = 7):
    stat_result = os.stat(path)
    content = Path(path).read_bytes()
    return validation.SnapshotValidationResult(
        ok=True,
        reason="ok",
        schema_version=schema_version,
        file_hash=hashlib.sha256(content).hexdigest(),
        file_size=stat_result.st_size,
        file_mtime=stat_result.st_mtime,
        fingerprint={
            "path": os.path.abspath(path),
            "size_bytes": stat_result.st_size,
            "mtime_ns": stat_result.st_mtime_ns,
            "schema_version": schema_version,
        },
    )


def test_attempt_reuses_unchanged_success_for_each_snapshot_target(tmp_path, monkeypatch):
    from rem_card.app import emergency_validation as validation

    medical = tmp_path / "medical.db"
    settings = tmp_path / "settings.db"
    medical.write_bytes(b"medical-snapshot")
    settings.write_bytes(b"settings-snapshot")
    calls = {"medical": 0, "settings": 0}
    metrics = []

    def validate_medical(path):
        calls["medical"] += 1
        return _successful_result(validation, path)

    def validate_settings(path):
        calls["settings"] += 1
        return _successful_result(validation, path, schema_version=3)

    monkeypatch.setattr(validation, "_validate_medical_db_snapshot_uncached", validate_medical)
    monkeypatch.setattr(validation, "_validate_settings_db_snapshot_uncached", validate_settings)
    monkeypatch.setattr(validation, "record_metric", lambda name, value, **fields: metrics.append((name, value, fields)))

    with validation.emergency_snapshot_validation_attempt():
        assert validation.validate_medical_db_snapshot(str(medical)).ok
        assert validation.validate_settings_db_snapshot(str(settings)).ok
        assert validation.validate_medical_db_snapshot(str(medical)).ok
        assert validation.validate_settings_db_snapshot(str(settings)).ok

    assert calls == {"medical": 1, "settings": 1}
    assert [entry[2]["target"] for entry in metrics] == ["medical", "settings"]
    assert {entry[2]["reason"] for entry in metrics} == {"unchanged_success_within_startup_attempt"}
    assert all("path" not in entry[2] for entry in metrics)


def test_mutated_and_replaced_snapshot_are_revalidated(tmp_path, monkeypatch):
    from rem_card.app import emergency_validation as validation

    path = tmp_path / "medical.db"
    path.write_bytes(b"snapshot-one")
    calls = 0

    def validate(path_value):
        nonlocal calls
        calls += 1
        if not Path(path_value).is_file():
            return validation.SnapshotValidationResult(ok=False, reason="file does not exist")
        return _successful_result(validation, path_value)

    monkeypatch.setattr(validation, "_validate_medical_db_snapshot_uncached", validate)

    with validation.emergency_snapshot_validation_attempt():
        assert validation.validate_medical_db_snapshot(str(path)).ok
        path.write_bytes(b"snapshot-two")
        assert validation.validate_medical_db_snapshot(str(path)).ok

        previous_mtime_ns = path.stat().st_mtime_ns
        replacement = tmp_path / "replacement.db"
        replacement.write_bytes(b"snapshot-new")
        os.utime(replacement, ns=(previous_mtime_ns, previous_mtime_ns))
        os.replace(replacement, path)
        assert validation.validate_medical_db_snapshot(str(path)).ok

        path.unlink()
        assert not validation.validate_medical_db_snapshot(str(path)).ok

    assert calls == 4


def test_same_bytes_in_different_roots_do_not_share_validation(tmp_path, monkeypatch):
    from rem_card.app import emergency_validation as validation

    first = tmp_path / "root-a" / "medical.db"
    second = tmp_path / "root-b" / "medical.db"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"same-snapshot")
    second.write_bytes(b"same-snapshot")
    calls = 0

    def validate(path_value):
        nonlocal calls
        calls += 1
        return _successful_result(validation, path_value)

    monkeypatch.setattr(validation, "_validate_medical_db_snapshot_uncached", validate)

    with validation.emergency_snapshot_validation_attempt():
        assert validation.validate_medical_db_snapshot(str(first)).ok
        assert validation.validate_medical_db_snapshot(str(second)).ok

    assert calls == 2


def test_failed_validation_is_never_cached(tmp_path, monkeypatch):
    from rem_card.app import emergency_validation as validation

    path = tmp_path / "medical.db"
    path.write_bytes(b"broken")
    calls = 0

    def fail(_path):
        nonlocal calls
        calls += 1
        return validation.SnapshotValidationResult(ok=False, reason="quick_check failed")

    monkeypatch.setattr(validation, "_validate_medical_db_snapshot_uncached", fail)

    with validation.emergency_snapshot_validation_attempt():
        assert not validation.validate_medical_db_snapshot(str(path)).ok
        assert not validation.validate_medical_db_snapshot(str(path)).ok

    assert calls == 2


def test_new_attempt_starts_with_fresh_validation(tmp_path, monkeypatch):
    from rem_card.app import emergency_validation as validation

    path = tmp_path / "medical.db"
    path.write_bytes(b"snapshot")
    calls = 0

    def validate(path_value):
        nonlocal calls
        calls += 1
        return _successful_result(validation, path_value)

    monkeypatch.setattr(validation, "_validate_medical_db_snapshot_uncached", validate)

    with validation.emergency_snapshot_validation_attempt():
        assert validation.validate_medical_db_snapshot(str(path)).ok
        assert validation.validate_medical_db_snapshot(str(path)).ok
    with validation.emergency_snapshot_validation_attempt():
        assert validation.validate_medical_db_snapshot(str(path)).ok

    assert calls == 2


def test_snapshot_changed_during_validation_is_rejected(tmp_path, monkeypatch):
    from rem_card.app import emergency_validation as validation

    path = tmp_path / "medical.db"
    path.write_bytes(b"snapshot-one")

    def validate(path_value):
        Path(path_value).write_bytes(b"snapshot-two")
        return _successful_result(validation, path_value)

    monkeypatch.setattr(validation, "_validate_medical_db_snapshot_uncached", validate)

    with validation.emergency_snapshot_validation_attempt():
        result = validation.validate_medical_db_snapshot(str(path))

    assert not result.ok
    assert result.reason == "snapshot changed during validation"


def test_activation_reconfirms_current_standby_eligibility(monkeypatch):
    from rem_card.app import emergency_startup as startup

    expected = SimpleNamespace(generation_id="generation-a")
    create_calls = []

    class Store:
        def __init__(self, *, root, source_role):
            self.root = root
            self.source_role = source_role

        def resolve_root(self):
            return self.root

        def create_active_session_from_standby(self, metadata):
            create_calls.append(metadata)
            raise AssertionError("invalid standby must not be copied")

    class Manager:
        def validate_standby(self):
            return SimpleNamespace(ok=False, metadata=expected, reason="standby expired")

    monkeypatch.setattr(startup, "EmergencyLocalStore", Store)
    monkeypatch.setattr(startup, "EmergencyStandbyManager", Manager)
    decision = startup.EmergencyStartupDecision(
        role="nurse",
        allowed=True,
        status="standby_available",
        user_message="",
        root="isolated-root",
        standby_metadata=expected,
    )

    with pytest.raises(startup.EmergencyStoreError, match="больше не пригодна"):
        startup.start_or_resume_emergency_session(decision)

    assert create_calls == []
