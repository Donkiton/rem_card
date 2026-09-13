from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from rem_card.services import storage_maintenance as maintenance


@pytest.fixture
def root(tmp_path):
    (tmp_path / "archiv").mkdir()
    (tmp_path / "archiv/rao_journal.db").write_bytes(b"synthetic marker, not SQLite")
    return tmp_path


def old_file(root, relative, days=120, data=b"old diagnostic"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    stamp = time.time() - days * maintenance.DAY
    os.utime(path, (stamp, stamp))
    return path


def test_inspection_is_readonly_and_policy_is_narrow(root):
    removable = [
        "logs/audit_20260518.jsonl", "logs/metrics_20260518.jsonl",
        "logs/nurse_20260518.log", "backup_health/daily_backup_2026-05-18.done.json",
        "backup_health/startup_quick_check_state.json.tmp_1_2_12345",
        "locks/replica_snapshots/.PC_123_ab.lock.PC.456." + "a" * 32 + ".tmp",
        "quarantine/locks/PC_12_ab.lock.20260816_030033_188711.e3b0c44298fc.c3b72d22.malformed",
        "logs/diagnostics/crashes/processed/2026/05/" + "b" * 32 + ".json",
        "logs/diagnostics/crashes/summaries/crash-summary_20260518_120000.md",
    ]
    protected = [
        "logs/nurse_20260518_p123_s" + "a" * 32 + "_000001_active.log",
        "logs/audit_20261340.jsonl", "logs/unknown.jsonl",
        "logs/diagnostics/crashes/incoming/2026-05-18_" + "b" * 32 + ".json",
        "backup_health/startup_quick_check_state.json", "backup_health/daily_backup_2026-05-18.reserved.json",
        "backups/valid/old.db", "quarantine/shared_db/old.db", "snapshots/old.db",
        "session_locks/nurse.lock", "locks/replica_snapshots/active.lock",
        "settings/backup_health/invalid_backups/old.db", "logs/diagnostics/review.json",
    ]
    for relative in removable + protected:
        old_file(root, relative)
    before = sorted(str(p) for p in root.rglob("*"))
    result = maintenance.StorageMaintenanceService(root).inspect()
    assert not result.errors
    assert {x.relative_path for x in result.candidates} == set(removable)
    assert sorted(str(p) for p in root.rglob("*")) == before


@pytest.mark.skipif(os.name != "nt", reason="Windows exclusive deletion")
def test_clean_removes_only_unchanged_old_files_and_writes_report(root):
    old = old_file(root, "logs/audit_20260518.jsonl")
    changed = old_file(root, "logs/audit_20260519.jsonl")
    recent = old_file(root, "logs/audit_20260912.jsonl", days=1)
    service = maintenance.StorageMaintenanceService(root)
    preview = service.inspect()
    changed.write_bytes(b"new data")
    result = service.clean(preview)
    assert result['removed'] == 1, result
    assert not old.exists()
    assert changed.exists() and recent.exists()
    assert json.loads((root / "service_cleanup_last.json").read_text(encoding="utf-8"))['status'] == 'finished'
    assert not (root / maintenance.LOCK_NAME).exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows exclusive deletion")
def test_open_file_is_preserved(root):
    path = old_file(root, "logs/audit_20260518.jsonl")
    service = maintenance.StorageMaintenanceService(root)
    preview = service.inspect()
    with path.open("rb"):
        result = service.clean(preview)
    assert result['removed'] == 0
    assert path.exists()


def test_concurrent_cleanup_is_rejected(root):
    old_file(root, "logs/audit_20260518.jsonl")
    service = maintenance.StorageMaintenanceService(root)
    preview = service.inspect()
    (root / maintenance.LOCK_NAME).mkdir()
    with pytest.raises(RuntimeError, match="уже запущено"):
        service.clean(preview)
    assert (root / maintenance.LOCK_NAME).exists()


def test_forged_preview_cannot_delete_database(root):
    old_file(root, "logs/audit_20260518.jsonl")
    service = maintenance.StorageMaintenanceService(root)
    preview = service.inspect()
    preview.candidates = [replace(preview.candidates[0], relative_path="archiv/rao_journal.db")]
    assert service.clean(preview)['removed'] == 0
    assert (root / "archiv/rao_journal.db").exists()


def test_incomplete_preview_and_wrong_root_are_rejected(root, tmp_path):
    service = maintenance.StorageMaintenanceService(root)
    preview = service.inspect()
    preview.truncated = True
    with pytest.raises(ValueError):
        service.clean(preview)
    preview.truncated = False
    preview.root += "-other"
    with pytest.raises(ValueError):
        service.clean(preview)


def test_scan_limit_blocks_cleanup(root, monkeypatch):
    for day in (18, 19, 20):
        old_file(root, f"logs/audit_202605{day}.jsonl")
    monkeypatch.setattr(maintenance, 'MAX_SCAN', 2)
    service = maintenance.StorageMaintenanceService(root)
    preview = service.inspect()
    assert preview.truncated
    with pytest.raises(ValueError):
        service.clean(preview)


def test_report_failure_prevents_deletion(root, monkeypatch):
    path = old_file(root, "logs/audit_20260518.jsonl")
    service = maintenance.StorageMaintenanceService(root)
    def fail(payload):
        raise PermissionError("report denied")
    monkeypatch.setattr(service, '_report', fail)
    with pytest.raises(PermissionError):
        service.clean(service.inspect())
    assert path.exists()
    assert not (root / maintenance.LOCK_NAME).exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows exclusive deletion")
def test_replacement_after_reinspection_is_preserved(root, monkeypatch):
    path = old_file(root, "logs/audit_20260518.jsonl")
    service = maintenance.StorageMaintenanceService(root)
    preview = service.inspect()
    original = maintenance._delete_exclusive
    def replace_then_delete(candidate, item):
        candidate.unlink()
        candidate.write_bytes(b"new diagnostic content")
        return original(candidate, item)
    monkeypatch.setattr(maintenance, '_delete_exclusive', replace_then_delete)
    assert service.clean(preview)['removed'] == 0
    assert path.read_bytes() == b"new diagnostic content"


@pytest.mark.skipif(os.name != "nt", reason="Windows exclusive deletion")
def test_pass_limit_and_repeated_cleanup(root, monkeypatch):
    for day in (18, 19, 20):
        old_file(root, f"logs/audit_202605{day}.jsonl")
    service = maintenance.StorageMaintenanceService(root)
    monkeypatch.setattr(maintenance, 'MAX_DELETE', 2)
    first = service.clean(service.inspect())
    assert first['removed'] == 2 and first['remaining'] == 1
    assert service.clean(service.inspect())['removed'] == 1
    assert not service.inspect().candidates


@pytest.mark.skipif(os.name != "nt", reason="Windows exclusive deletion")
def test_final_report_failure_retains_actual_result(root, monkeypatch):
    old_file(root, "logs/audit_20260518.jsonl")
    service = maintenance.StorageMaintenanceService(root)
    original = service._report
    def fail_final(payload):
        if payload['status'] == 'finished':
            raise PermissionError('network lost after deletion')
        original(payload)
    monkeypatch.setattr(service, '_report', fail_final)
    result = service.clean(service.inspect())
    assert result['removed'] == 1
    assert not result['report_written']
    assert result['errors']


@pytest.mark.parametrize('name,days', [
    ('logs/audit_20260518.jsonl', 90),
    ('logs/metrics_20260518.jsonl', 14),
    ('logs/nurse_20260518.log', 30),
])
def test_retention_boundary(root, name, days):
    path = old_file(root, name, days=days)
    modified = path.stat().st_mtime
    service = maintenance.StorageMaintenanceService(root)
    assert not service.inspect(now=modified + days * maintenance.DAY).candidates
    assert len(service.inspect(now=modified + days * maintenance.DAY + 1).candidates) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows junction")
def test_junction_is_not_followed(root, tmp_path):
    import subprocess
    outside = tmp_path / "outside"
    outside.mkdir()
    target = old_file(outside, "audit_20260518.jsonl")
    junction = root / "logs"
    subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(outside)], check=True, capture_output=True)
    try:
        result = maintenance.StorageMaintenanceService(root).inspect()
        assert not result.candidates
        assert result.errors
        assert target.exists()
    finally:
        junction.rmdir()
