from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.services import crash_reports


def _same_failure():
    try:
        raise RuntimeError(r"patient secret at C:\private\patient\record.db")
    except RuntimeError:
        return sys.exc_info()


def _payloads(outbox_root: Path) -> list[dict]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(outbox_root.glob("*.json"))]


def test_exception_reports_are_private_stable_and_delivered_to_arbitrary_root(tmp_path, monkeypatch):
    spool = tmp_path / "local-spool"
    data_root = tmp_path / "любое имя папки данных"
    monkeypatch.setenv("REMCARD_CRASH_OUTBOX_DIR", str(spool))

    for _ in range(2):
        exc_type, exc_value, exc_traceback = _same_failure()
        crash_reports.capture_exception(
            "unhandled_python_exception",
            exc_type,
            exc_value,
            exc_traceback,
            role="doctor",
        )

    payloads = _payloads(spool / "outbox")
    assert len(payloads) == 2
    assert payloads[0]["fingerprint"] == payloads[1]["fingerprint"]
    serialized = json.dumps(payloads, ensure_ascii=False)
    assert "patient secret" not in serialized
    assert r"C:\private" not in serialized
    assert payloads[0]["frames"][-1]["file"].endswith("test_crash_reports.py")

    delivered = crash_reports.flush_local_crash_outbox(data_root)
    assert delivered == {"delivered": 2, "failed": 0, "remaining": 0}
    incoming = data_root / "logs" / "diagnostics" / "crashes" / "incoming"
    assert len(list(incoming.glob("*.json"))) == 2
    sent = list((spool / "sent").glob("*.json"))
    assert len(sent) == 2
    assert _payloads(spool / "outbox") == []


def test_clean_session_creates_no_crash_report(tmp_path, monkeypatch):
    spool = tmp_path / "spool"
    monkeypatch.setenv("REMCARD_CRASH_OUTBOX_DIR", str(spool))
    monkeypatch.setattr(crash_reports.faulthandler, "enable", lambda **_kwargs: None)
    monkeypatch.setattr(crash_reports.faulthandler, "disable", lambda: None)

    crash_reports.initialize_crash_session(role="nurse")
    crash_reports.finalize_crash_session(exit_code=0)

    assert list((spool / "outbox").glob("*.json")) == [] if (spool / "outbox").exists() else True
    assert list((spool / "sessions").glob("*.json")) == []


def test_stale_native_session_creates_one_native_report_without_generic_duplicate(tmp_path, monkeypatch):
    spool = tmp_path / "spool"
    monkeypatch.setenv("REMCARD_CRASH_OUTBOX_DIR", str(spool))
    monkeypatch.setattr(crash_reports, "_pid_is_running", lambda _pid: False)
    monkeypatch.setattr(crash_reports.faulthandler, "enable", lambda **_kwargs: None)
    monkeypatch.setattr(crash_reports.faulthandler, "disable", lambda: None)
    native_path = spool / "native" / "old.log"
    native_path.parent.mkdir(parents=True)
    native_path.write_text(r"Fatal Python error at C:\Users\Person\app.py", encoding="utf-8")
    marker = spool / "sessions" / "old.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps(
            {
                "session_id": "old-session",
                "pid": 999999,
                "role": "doctor",
                "native_path": str(native_path),
            }
        ),
        encoding="utf-8",
    )

    crash_reports.initialize_crash_session(role="doctor")
    crash_reports.finalize_crash_session(exit_code=0)

    payloads = _payloads(spool / "outbox")
    assert [payload["event_type"] for payload in payloads] == ["native_crash"]
    assert payloads[0]["details"]["previous_session_unclean"] is True
    assert r"C:\Users" not in json.dumps(payloads, ensure_ascii=False)


def test_database_outage_is_deduplicated_per_process(tmp_path, monkeypatch):
    monkeypatch.setenv("REMCARD_CRASH_OUTBOX_DIR", str(tmp_path / "spool"))
    monkeypatch.setattr(crash_reports.time, "monotonic", lambda: 15.0)
    crash_reports._DATABASE_LAST_REPORTED.clear()
    first = crash_reports.capture_database_failure(
        "network_unavailable",
        role="nurse",
        phase="runtime",
        check_result=r"unable to open Z:\secret\rao_journal.db",
    )
    second = crash_reports.capture_database_failure(
        "network_unavailable",
        role="nurse",
        phase="runtime",
        check_result=r"unable to open Z:\secret\rao_journal.db",
    )
    assert first is not None
    assert second is None
    payload = json.loads(first.read_text(encoding="utf-8"))
    assert payload["event_type"] == "database_unavailable_runtime"
    assert r"Z:\secret" not in json.dumps(payload, ensure_ascii=False)


def test_crash_atomic_write_retries_transient_windows_replace_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("REMCARD_CRASH_OUTBOX_DIR", str(tmp_path / "spool"))
    original_replace = crash_reports.os.replace
    attempts = {"count": 0}

    def transient_replace(source, target):
        if attempts["count"] == 0:
            attempts["count"] += 1
            raise PermissionError("simulated antivirus lock")
        return original_replace(source, target)

    monkeypatch.setattr(crash_reports.os, "replace", transient_replace)
    path = crash_reports.capture_crash_event("previous_session_unclean")

    assert attempts["count"] == 1
    assert path is not None and path.is_file()


def test_failed_database_report_does_not_poison_deduplication(monkeypatch):
    monkeypatch.setattr(crash_reports.time, "monotonic", lambda: 15.0)
    crash_reports._DATABASE_LAST_REPORTED.clear()
    expected = Path("retry.json")
    results = iter([None, expected])
    monkeypatch.setattr(crash_reports, "capture_crash_event", lambda *_args, **_kwargs: next(results))

    assert crash_reports.capture_database_failure("network_unavailable", phase="runtime") is None
    assert crash_reports.capture_database_failure("network_unavailable", phase="runtime") == expected


def test_runtime_logs_directory_is_local_and_does_not_resolve_selected_root(tmp_path, monkeypatch):
    from rem_card.app import runtime_paths

    local_appdata = tmp_path / "local-appdata"
    program_dir = tmp_path / "program"
    monkeypatch.delenv("REMCARD_LOCAL_LOGS_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
    monkeypatch.setattr(runtime_paths, "is_compiled", lambda: True)
    monkeypatch.setattr(runtime_paths, "get_executable_dir", lambda: str(program_dir))
    monkeypatch.setattr(
        runtime_paths,
        "resolve_baza_dir",
        lambda: (_ for _ in ()).throw(AssertionError("network data root must not be resolved")),
    )
    assert Path(runtime_paths.get_runtime_logs_dir()) == program_dir / "logs"


@pytest.mark.parametrize(
    ("executable_name", "argv", "expected"),
    [
        ("RemCardDoctor.exe", ["RemCardDoctor.exe"], "doctor"),
        ("RemCardNurse.exe", ["RemCardNurse.exe"], "nurse"),
        ("RemCardOperBlock.exe", ["RemCardOperBlock.exe"], "operblock"),
        (
            "RemCardOperBlockEmergency.exe",
            ["RemCardOperBlockEmergency.exe"],
            "operblock_emergency",
        ),
        (
            "RemCardOperBlockPlanned.exe",
            ["RemCardOperBlockPlanned.exe"],
            "operblock_planned",
        ),
        ("RemCardPathSetup.exe", ["RemCardPathSetup.exe"], "path_setup"),
    ],
)
def test_every_compiled_role_keeps_a_distinct_local_log_prefix(
    monkeypatch,
    executable_name,
    argv,
    expected,
):
    from rem_card.app import runtime_paths

    monkeypatch.delenv("REMCARD_LOG_PREFIX", raising=False)
    monkeypatch.setattr(runtime_paths.sys, "executable", executable_name)
    monkeypatch.setattr(runtime_paths.sys, "argv", argv)

    assert runtime_paths.get_log_file_prefix() == expected


def test_unwritable_program_logs_use_explicit_local_fallback(tmp_path, monkeypatch):
    from rem_card.app import runtime_paths

    preferred = str(tmp_path / "program" / "logs")
    fallback = str(tmp_path / "temp" / "RemCard" / "logs")
    recorded = {}
    monkeypatch.setattr(
        runtime_paths,
        "get_runtime_log_directory_candidates",
        lambda: (preferred, fallback),
    )
    monkeypatch.setattr(
        runtime_paths,
        "_runtime_log_directory_is_writable",
        lambda path: (False, "access denied") if path == preferred else (True, ""),
    )
    monkeypatch.setattr(
        runtime_paths,
        "record_runtime_log_location",
        lambda effective_dir, **kwargs: recorded.update(
            {"effective_dir": effective_dir, **kwargs}
        ),
    )
    monkeypatch.setattr(runtime_paths, "_RUNTIME_LOG_LOCATION_CACHE", None)

    assert runtime_paths.get_writable_runtime_logs_dir() == fallback
    assert recorded["effective_dir"] == fallback
    assert recorded["preferred_dir"] == preferred
    assert "access denied" in recorded["fallback_reason"]


def test_legacy_appdata_logs_are_copied_once_without_deleting_sources(tmp_path, monkeypatch):
    from rem_card.app import runtime_paths

    local_appdata = tmp_path / "local-appdata"
    target = tmp_path / "program" / "logs"
    legacy_log = local_appdata / "RemCard" / "logs" / "doctor_20260724.log"
    legacy_crash = local_appdata / "RemCard" / "crash-outbox" / "outbox" / "old.json"
    legacy_log.parent.mkdir(parents=True)
    legacy_crash.parent.mkdir(parents=True)
    legacy_log.write_text("старый лог", encoding="utf-8")
    legacy_crash.write_text('{"id": "old"}', encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))

    first = runtime_paths.migrate_legacy_runtime_logs(str(target))
    second = runtime_paths.migrate_legacy_runtime_logs(str(target))

    assert first["copied"] == 2
    assert first["errors"] == []
    assert second["skipped"] is True
    assert legacy_log.read_text(encoding="utf-8") == "старый лог"
    assert legacy_crash.is_file()
    assert (
        target / "migrated-appdata" / "logs" / legacy_log.name
    ).read_text(encoding="utf-8") == "старый лог"
    assert (
        target / "migrated-appdata" / "crash-outbox" / "outbox" / legacy_crash.name
    ).is_file()


def test_default_crash_spool_is_inside_active_local_logs(tmp_path, monkeypatch):
    logs_dir = tmp_path / "program" / "logs"
    monkeypatch.delenv("REMCARD_CRASH_OUTBOX_DIR", raising=False)
    monkeypatch.setattr(
        crash_reports,
        "get_writable_runtime_logs_dir",
        lambda: str(logs_dir),
    )

    assert crash_reports.get_local_crash_spool_dir() == logs_dir / "crashes"


def test_legacy_crash_outbox_is_imported_without_deleting_appdata_copy(tmp_path, monkeypatch):
    local_appdata = tmp_path / "local-appdata"
    legacy_spool = local_appdata / "RemCard" / "crash-outbox"
    active_logs = tmp_path / "program" / "logs"
    source = legacy_spool / "outbox" / "pending.json"
    source.parent.mkdir(parents=True)
    source.write_text('{"schema_version": 1, "id": "pending"}', encoding="utf-8")
    monkeypatch.delenv("REMCARD_CRASH_OUTBOX_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
    monkeypatch.setattr(crash_reports, "is_compiled", lambda: True)
    monkeypatch.setattr(
        crash_reports,
        "get_writable_runtime_logs_dir",
        lambda: str(active_logs),
    )

    result = crash_reports.migrate_legacy_crash_spool()

    assert result["outbox"] == 1
    assert result["failed"] == 0
    assert source.is_file()
    assert (active_logs / "crashes" / "outbox" / source.name).is_file()
    assert (active_logs / "crashes" / ".appdata-import-v1.json").is_file()


def test_sent_crash_retention_never_removes_pending_reports(tmp_path, monkeypatch):
    spool = tmp_path / "spool"
    monkeypatch.setenv("REMCARD_CRASH_OUTBOX_DIR", str(spool))
    sent_old = spool / "sent" / "old.json"
    sent_new = spool / "sent" / "new.json"
    pending_old = spool / "outbox" / "pending.json"
    for path in (sent_old, sent_new, pending_old):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    old_ts = time.time() - 10 * 86400
    os.utime(sent_old, (old_ts, old_ts))
    os.utime(pending_old, (old_ts, old_ts))

    removed = crash_reports.cleanup_old_local_crash_reports(retention_days=5)

    assert removed == 1
    assert not sent_old.exists()
    assert sent_new.exists()
    assert pending_old.exists()


def test_stale_marker_cannot_read_or_delete_native_path_outside_spool(tmp_path, monkeypatch):
    spool = tmp_path / "spool"
    victim = tmp_path / "patient-archive.db"
    victim.write_text("must survive", encoding="utf-8")
    monkeypatch.setenv("REMCARD_CRASH_OUTBOX_DIR", str(spool))
    monkeypatch.setattr(crash_reports, "_pid_is_running", lambda _pid: False)
    monkeypatch.setattr(crash_reports.faulthandler, "enable", lambda **_kwargs: None)
    monkeypatch.setattr(crash_reports.faulthandler, "disable", lambda: None)

    marker = spool / "sessions" / "tampered.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps({"session_id": "tampered", "pid": 999999, "native_path": str(victim)}),
        encoding="utf-8",
    )

    crash_reports.initialize_crash_session(role="doctor")
    crash_reports.finalize_crash_session(exit_code=0)

    assert victim.read_text(encoding="utf-8") == "must survive"
    assert not marker.exists()
    payloads = _payloads(spool / "outbox")
    assert [payload["event_type"] for payload in payloads] == ["previous_session_unclean"]


def test_recorded_exception_does_not_create_generic_shutdown_duplicate(tmp_path, monkeypatch):
    spool = tmp_path / "spool"
    monkeypatch.setenv("REMCARD_CRASH_OUTBOX_DIR", str(spool))
    monkeypatch.setattr(crash_reports.faulthandler, "enable", lambda **_kwargs: None)
    monkeypatch.setattr(crash_reports.faulthandler, "disable", lambda: None)

    crash_reports.initialize_crash_session(role="doctor")
    exc_type, exc_value, exc_traceback = _same_failure()
    recorded = crash_reports.capture_exception(
        "unhandled_python_exception",
        exc_type,
        exc_value,
        exc_traceback,
        role="doctor",
    )
    crash_reports.finalize_crash_session(exit_code=1, crash_recorded=recorded is not None)

    payloads = _payloads(spool / "outbox")
    assert [payload["event_type"] for payload in payloads] == ["unhandled_python_exception"]
