from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR.parent) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR.parent))

from rem_card.app import runtime_log_retention as retention
from rem_card.app import runtime_log_storage as storage
from rem_card.app import runtime_paths


@pytest.fixture(autouse=True)
def isolated_logs(tmp_path, monkeypatch):
    storage.close_log_writers()
    monkeypatch.setenv("REMCARD_LOG_STORAGE_ENABLED", "1")
    monkeypatch.setenv("REMCARD_LOG_MAX_FILE_BYTES", "1048576")
    monkeypatch.setenv("REMCARD_LOG_TOTAL_BYTES", "10485760")
    monkeypatch.setenv("REMCARD_LOG_CLEANUP_DRY_RUN", "0")
    monkeypatch.setenv("REMCARD_LOCAL_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(runtime_paths, "get_runtime_log_directory_candidates", lambda: (
        str(tmp_path / "logs"), str(tmp_path / "fallback"),
    ))
    monkeypatch.setattr(storage, "_request_cleanup", lambda *_args: None)
    yield
    storage.close_log_writers()


def old_file(root, name, *, days=10, size=100):
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_bytes(b"x" * size)
    timestamp = time.time() - days * 86400
    os.utime(path, (timestamp, timestamp))
    return path


def read_jsonl(root):
    return [json.loads(line) for path in sorted(root.glob("metrics_*.jsonl"))
            for line in path.read_text(encoding="utf-8").splitlines()]


def test_rotation_preserves_utf8_records_and_readable_suffixes(tmp_path, monkeypatch):
    monkeypatch.setenv("REMCARD_LOG_MAX_FILE_BYTES", "150")
    root = tmp_path / "logs"
    expected = [{"metric": "проверка", "value": index} for index in range(24)]
    storage.append_log_lines(root, "metrics", (json.dumps(row, ensure_ascii=False) + "\n" for row in expected), extension="jsonl")
    paths = list(root.glob("metrics_*.jsonl"))
    assert len(paths) > 1
    assert all(path.stat().st_size <= 150 for path in paths)
    assert sum("_active." in path.name for path in paths) == 1
    assert read_jsonl(root) == expected


def test_midnight_rotates_even_without_restart(tmp_path, monkeypatch):
    class Clock:
        current = datetime(2026, 8, 31, 23, 59, 59)

        @classmethod
        def now(cls):
            return cls.current

    monkeypatch.setattr(storage, "datetime", Clock)
    root = tmp_path / "logs"
    first = storage.append_log_lines(root, "startup", ["before\n"])
    Clock.current += timedelta(seconds=2)
    second = storage.append_log_lines(root, "startup", ["after\n"])
    assert "20260831" in first.name
    assert "20260901" in second.name
    assert sorted(p.read_text() for p in root.glob("startup_*.log")) == ["after\n", "before\n"]


def test_oversized_single_event_is_preserved_not_split(tmp_path, monkeypatch):
    monkeypatch.setenv("REMCARD_LOG_MAX_FILE_BYTES", "40")
    row = {"metric": "large", "value": "x" * 100}
    storage.append_log_lines(tmp_path, "metrics", [json.dumps(row) + "\n", '{"metric":"next"}\n'], extension="jsonl")
    assert read_jsonl(tmp_path) == [row, {"metric": "next"}]
    assert len(list(tmp_path.glob("metrics_*.jsonl"))) == 2


def test_rollback_keeps_legacy_names_and_payloads(tmp_path, monkeypatch):
    monkeypatch.setenv("REMCARD_LOG_STORAGE_ENABLED", "0")
    storage.append_log_lines(tmp_path, "startup", ["a\n", "b\n"])
    storage.append_log_lines(tmp_path, "metrics", ['{"metric":"probe"}\n'], extension="jsonl")
    assert (tmp_path / "startup.log").read_text() == "a\nb\n"
    assert (tmp_path / f"metrics_{datetime.now():%Y%m%d}.jsonl").is_file()


def test_known_files_only_and_distinct_retention_windows(tmp_path):
    root = tmp_path / "logs"
    removed = [old_file(root, "doctor_20260101.log", days=31),
               old_file(root, "metrics_20260101.jsonl", days=8),
               old_file(root, "audit_20260101.jsonl", days=91)]
    kept = [old_file(root, "nurse_20260101.log", days=29),
            old_file(root, "metrics_20260102.jsonl", days=6),
            old_file(root, "audit_20260102.jsonl", days=89),
            old_file(root, "important.txt", days=200),
            old_file(root, "medical_audit_log.jsonl", days=200),
            old_file(root, "rao_journal.db", days=200),
            old_file(root, "metrics_20269999.jsonl", days=200),
            old_file(root / "crashes" / "outbox", "pending.json", days=200),
            old_file(root / "crashes" / "native", "native.log", days=200),
            old_file(root / "other", "doctor_20260101.log", days=200)]
    result = retention.cleanup_logs([str(root)])
    assert result["removed_files"] == 3
    assert result["freed_bytes"] == 300
    assert all(not p.exists() for p in removed)
    assert all(p.exists() for p in kept)


def test_combined_quota_includes_fallback_and_migrated_logs_but_not_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("REMCARD_LOG_TOTAL_BYTES", "150")
    root, fallback = tmp_path / "logs", tmp_path / "fallback"
    first = old_file(root / "migrated-appdata" / "logs", "doctor_20260801.log", days=3)
    second = old_file(fallback, "metrics_20260802.jsonl", days=2)
    last = old_file(root, "nurse_20260803.log", days=1)
    audit = old_file(root, "audit_20260803.jsonl", days=1, size=500)
    result = retention.cleanup_logs([str(root), str(fallback)])
    assert result["ordinary_bytes"] == 100
    assert not result["over_budget"]
    assert not first.exists() and not second.exists()
    assert last.exists() and audit.exists()


def test_live_active_segment_is_protected_but_closed_segment_is_reclaimable(tmp_path, monkeypatch):
    monkeypatch.setenv("REMCARD_LOG_TOTAL_BYTES", "1")
    monkeypatch.setenv("REMCARD_LOG_MAX_FILE_BYTES", "10")
    storage.append_log_lines(tmp_path, "doctor", ["first-line\n", "next-line\n"])
    active = next(tmp_path.glob("*_active.log"))
    closed = next(tmp_path.glob("*_closed.log"))
    result = retention.cleanup_logs([str(tmp_path)])
    assert active.exists() and not closed.exists()
    assert result["over_budget"]


def test_dead_process_segments_are_reclaimable(tmp_path, monkeypatch):
    monkeypatch.setattr(retention, "_pid_running", lambda _pid: False)
    path = old_file(tmp_path, "metrics_20260101_p999999_s" + "a" * 32 + "_000001_active.jsonl")
    assert retention.cleanup_logs([str(tmp_path)])["removed_files"] == 1
    assert not path.exists()


def test_unverifiable_owner_does_not_abort_cleanup_of_other_files(tmp_path):
    unknown = old_file(tmp_path, "metrics_20260101_p999999999999_s" + "a" * 32 + "_000001_active.jsonl")
    stale = old_file(tmp_path, "metrics_20260101.jsonl")
    result = retention.cleanup_logs([str(tmp_path)])
    assert result["removed_files"] == 1
    assert unknown.exists() and not stale.exists()


def test_inherited_writer_cannot_seal_parent_segment(tmp_path, monkeypatch):
    writer = storage.LogSegmentWriter(str(tmp_path), "doctor", "log", managed=False)
    active = writer.write(["parent\n"])
    parent_pid = os.getpid()
    with monkeypatch.context() as patch:
        patch.setattr(storage.os, "getpid", lambda: parent_pid + 1)
        writer.close()
    assert active.exists()
    writer.close()
    assert not active.exists()


def test_preview_never_deletes_and_apply_preserves_changed_files(tmp_path):
    changed = old_file(tmp_path, "metrics_20260101.jsonl")
    untouched = old_file(tmp_path, "metrics_20260102.jsonl")
    plan = retention.cleanup_logs([str(tmp_path)], dry_run=True)
    assert changed.exists() and untouched.exists()
    changed.write_text("new diagnostic information")
    result = retention.apply_log_cleanup(plan, [str(tmp_path)])
    assert result["removed_files"] == 1
    assert changed.read_text() == "new diagnostic information"
    assert not untouched.exists()


def test_forged_preview_cannot_delete_unknown_files_or_escape_root(tmp_path):
    root = tmp_path / "logs"
    safe = old_file(root, "metrics_20260101.jsonl")
    secret = old_file(tmp_path / "outside", "metrics_20260101.jsonl")
    plan = retention.plan_log_cleanup([str(root)])
    plan["candidates"].append({**plan["candidates"][0], "path": str(secret)})
    result = retention.apply_log_cleanup(plan, [str(root)])
    assert not safe.exists() and secret.exists()
    assert result["skipped_files"] == 1


def test_symlinks_and_windows_junctions_are_not_followed(tmp_path):
    outside = tmp_path / "outside"
    protected = old_file(outside, "metrics_20260101.jsonl")
    root = tmp_path / "logs"
    root.mkdir()
    link = root / "migrated-appdata"
    if os.name == "nt":
        # Only create a directory junction; no shell deletion or moving.
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
        assert result.returncode == 0
    else:
        link.symlink_to(outside, target_is_directory=True)
    retention.cleanup_logs([str(link)])
    retention.cleanup_logs([str(root)])
    assert protected.exists()


def test_failed_delete_is_reported_and_other_files_are_cleaned(tmp_path, monkeypatch):
    locked = old_file(tmp_path, "metrics_20260101.jsonl")
    other = old_file(tmp_path, "metrics_20260102.jsonl")
    original = Path.unlink

    def unlink(path, *args, **kwargs):
        if path == locked:
            raise PermissionError("file is in use")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    result = retention.cleanup_logs([str(tmp_path)])
    assert result["failed_files"] == 1
    assert locked.exists() and not other.exists()


def test_protected_crash_budget_warns_without_deleting(tmp_path, monkeypatch):
    monkeypatch.setenv("REMCARD_LOG_TOTAL_BYTES", "50")
    pending = old_file(tmp_path / "crashes" / "outbox", "pending.json", size=100)
    retention.run_log_maintenance([str(tmp_path)])
    assert pending.exists()
    summary = next(tmp_path.glob("log_maintenance_*.log")).read_text()
    assert "WARNING" in summary and '"protected_crash_over_budget": true' in summary


def test_periodic_cleanup_runs_without_backup_or_more_log_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("REMCARD_LOG_CLEANUP_INTERVAL_SEC", "1")
    root = tmp_path / "logs"
    root.mkdir()
    retention.request_log_cleanup(str(root))
    deadline = time.monotonic() + 5
    while not list(root.glob("log_maintenance_*.log")) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert list(root.glob("log_maintenance_*.log"))
    late = old_file(root, "metrics_20260101.jsonl")
    deadline = time.monotonic() + 5
    while late.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not late.exists()


def test_concurrent_process_metrics_are_valid_complete_and_rotated(tmp_path):
    root = tmp_path / "logs"
    env = dict(os.environ, PYTHONPATH=str(PROJECT_DIR.parent),
               REMCARD_LOCAL_LOGS_DIR=str(root), REMCARD_LOG_MAX_FILE_BYTES="4096",
               REMCARD_LOCAL_METRICS_ENABLED="1", REMCARD_LOCAL_METRICS_SYNC="1")
    code = (
        "from rem_card.app.local_metrics import record_metric\n"
        "import os\n"
        "for i in range(150): record_metric('parallel_probe', i, worker=os.getpid())\n"
    )
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    processes = [subprocess.Popen([sys.executable, "-B", "-c", code], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 creationflags=flags) for _ in range(3)]
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, (stdout, stderr)
            assert b"Traceback" not in stderr, stderr
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    rows = read_jsonl(root)
    assert len(rows) == 450
    assert len({(row["worker"], row["value"]) for row in rows}) == 450
    assert len({row["worker"] for row in rows}) == 3
    assert all(path.stat().st_size <= 4096 for path in root.glob("metrics_*.jsonl"))


def test_storage_paths_never_resolve_database(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_paths, "resolve_baza_dir", lambda: pytest.fail("must not inspect DB"))
    storage.append_log_lines(tmp_path, "doctor", ["probe\n"])
    retention.cleanup_logs([str(tmp_path)])


def test_prefix_cannot_escape_log_directory(tmp_path):
    path = storage.append_log_lines(tmp_path, "../../outside", ["probe\n"])
    assert path.parent == tmp_path
    assert path.name.startswith("runtime_")


def test_writer_survives_disk_error_without_joining_partial_json(tmp_path, monkeypatch):
    writer = storage.LogSegmentWriter(str(tmp_path), "metrics", "jsonl", managed=False)
    writer.write(['{"metric":"before"}\n'])
    original = Path.open

    def fail_append(path, mode="r", *args, **kwargs):
        if mode == "ab":
            raise OSError("disk full")
        return original(path, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", fail_append)
        with pytest.raises(OSError, match="disk full"):
            writer.write(['{"metric":"failed"}\n'])
    writer.write(['{"metric":"after"}\n'])
    assert read_jsonl(tmp_path) == [{"metric": "before"}, {"metric": "after"}]
    writer.close()


def test_manual_cleanup_requires_matching_reviewed_scope(tmp_path):
    from rem_card.scripts.manage_runtime_logs import main

    root = tmp_path / "logs"
    path = old_file(root, "metrics_20260101.jsonl")
    plan = tmp_path / "preview.json"
    assert main(["--logs-dir", str(root), "--preview", str(plan)]) == 0
    assert path.exists()
    with pytest.raises(SystemExit):
        main(["--logs-dir", str(tmp_path), "--apply", str(plan)])
    assert path.exists()
    assert main(["--logs-dir", str(root), "--apply", str(plan)]) == 0
    assert not path.exists()


def test_automatic_dry_run_reports_candidates_without_deleting(tmp_path, monkeypatch):
    monkeypatch.setenv("REMCARD_LOG_CLEANUP_DRY_RUN", "1")
    candidate = old_file(tmp_path, "metrics_20260101.jsonl")
    result = retention.run_log_maintenance([str(tmp_path)])
    assert len(result["candidates"]) == 1
    assert candidate.exists()
    summary = next(tmp_path.glob("log_maintenance_*.log")).read_text()
    assert '"dry_run": true' in summary


def test_existing_analyzers_and_user_reports_read_rotated_and_legacy_files(tmp_path, monkeypatch):
    from rem_card.scripts import analyze_opblock_idle_stalls, analyze_ui_stall_logs
    from rem_card.services.user_reports import UserReportsService

    monkeypatch.setenv("REMCARD_LOG_MAX_FILE_BYTES", "200")
    now = datetime.now().replace(microsecond=0)
    rows = [{"ts": now.isoformat(), "metric": "sqlite_write_lock_acquired", "value": i} for i in range(8)]
    storage.append_log_lines(tmp_path, "metrics", (json.dumps(row) + "\n" for row in rows), extension="jsonl")
    legacy = tmp_path / f"metrics_{now:%Y%m%d}.jsonl"
    legacy.write_text(json.dumps({**rows[0], "value": 8}) + "\n", encoding="utf-8")
    events = analyze_ui_stall_logs._load_events(tmp_path, (now.strftime("%Y%m%d"), now.strftime("%Y-%m-%d")), None)
    assert sorted(event.value for event in events if event.kind == "sqlite_write_lock_acquired") == list(range(9))
    events = analyze_opblock_idle_stalls._load_events(tmp_path, now.strftime("%Y-%m-%d"))
    assert sorted(event.value for event in events if event.kind == "sqlite_write_lock_acquired") == list(range(9))
    storage.append_log_lines(tmp_path, "doctor", [
        f"{now:%Y-%m-%d %H:%M:%S} | INFO | RemCard | diagnostic-line-{i}\n" for i in range(8)
    ])
    service = UserReportsService(reports_root=tmp_path / "reports", logs_dirs=[tmp_path])
    text = service.collect_logs_for_period(now - timedelta(minutes=1), now + timedelta(minutes=1))
    assert all(f"diagnostic-line-{i}" in text for i in range(8))
