from __future__ import annotations

import json
import os
import sys
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


def test_runtime_logs_directory_uses_selected_root_name(tmp_path, monkeypatch):
    from rem_card.app import runtime_paths

    selected = tmp_path / "Журнал отделения"
    monkeypatch.delenv("REMCARD_LOCAL_LOGS_DIR", raising=False)
    monkeypatch.setattr(runtime_paths, "resolve_baza_dir", lambda: str(selected))
    assert Path(runtime_paths.get_runtime_logs_dir()) == selected / "logs"
