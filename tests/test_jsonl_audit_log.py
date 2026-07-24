from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.app import jsonl_audit_log


def test_audit_is_written_locally_before_shared_mirror(tmp_path, monkeypatch):
    assert jsonl_audit_log.flush_audit_mirror(timeout_sec=1.0)
    local_logs = tmp_path / "program" / "logs"
    shared_root = tmp_path / "database"
    shared_started = threading.Event()
    release_shared = threading.Event()
    original_append = jsonl_audit_log._append_jsonl

    monkeypatch.setattr(
        jsonl_audit_log,
        "get_writable_runtime_logs_dir",
        lambda: str(local_logs),
    )

    def delayed_shared_append(path, payload):
        if Path(path).is_relative_to(shared_root):
            shared_started.set()
            release_shared.wait(timeout=2.0)
        return original_append(path, payload)

    monkeypatch.setattr(jsonl_audit_log, "_append_jsonl", delayed_shared_append)

    started_at = time.monotonic()
    jsonl_audit_log.write_audit_event(
        "startup_test",
        baza_dir=str(shared_root),
        role="doctor",
        details={"phase": "test"},
    )
    elapsed = time.monotonic() - started_at

    local_files = list(local_logs.glob("audit_*.jsonl"))
    assert elapsed < 0.25
    assert len(local_files) == 1
    local_payload = json.loads(local_files[0].read_text(encoding="utf-8").strip())
    assert local_payload["event"] == "startup_test"
    assert local_payload["phase"] == "test"
    assert shared_started.wait(timeout=1.0)

    release_shared.set()
    assert jsonl_audit_log.flush_audit_mirror(timeout_sec=1.0)
    shared_files = list((shared_root / "logs").glob("audit_*.jsonl"))
    assert len(shared_files) == 1


def test_local_audit_does_not_touch_database_when_root_is_not_supplied(tmp_path, monkeypatch):
    local_logs = tmp_path / "program" / "logs"
    monkeypatch.setattr(
        jsonl_audit_log,
        "get_writable_runtime_logs_dir",
        lambda: str(local_logs),
    )
    monkeypatch.setattr(
        jsonl_audit_log,
        "_enqueue_audit_mirror",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("shared mirror must not be used")
        ),
    )

    jsonl_audit_log.write_audit_event("local_only")

    assert len(list(local_logs.glob("audit_*.jsonl"))) == 1
