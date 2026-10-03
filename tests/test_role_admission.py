import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from types import SimpleNamespace

import pytest

from rem_card.app import role_admission as admission, startup_db_guard as guard
from rem_card.app.startup_check_worker import StartupCheckAborted, run_startup_probe, startup_check_runner


@pytest.fixture(autouse=True)
def clean_admission():
    admission.invalidate_role_admission()
    yield
    admission.invalidate_role_admission()


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / "archiv" / "rao_journal.db"
    path.parent.mkdir()
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE example(value TEXT)")
        conn.execute("INSERT INTO example VALUES ('synthetic')")
        conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO schema_migrations VALUES (1)")
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    monkeypatch.setattr(guard, "resolve_baza_dir", lambda: str(tmp_path))
    monkeypatch.setattr(guard, "_load_or_create_client_policy", lambda *args: None)
    monkeypatch.setattr(guard, "write_audit_event", lambda *args, **kwargs: None)
    return path


def _runner():
    calls = []
    def check(path):
        calls.append("full")
        check.admission_receipt = None
        ok, result, corrupt = guard._check_quick_direct(path, capture_admission=True)
        if ok and result != "ok":
            check.admission_receipt = json.loads(result)
            result = "ok"
        return ok, result, corrupt
    def light(path):
        calls.append("light")
        ok, result, _ = admission.read_only_admission_probe(path)
        return json.loads(result) if ok else None
    check.admission_probe = light
    check.check_cancelled = lambda: None
    check.calls = calls
    return check


def _enter(runner, role="doctor"):
    with startup_check_runner(runner):
        return guard.run_startup_db_guard(role)


def test_ordinary_writes_reuse_full_scan_and_keep_original_timestamp(database):
    runner = _runner()
    assert _enter(runner).ok
    first, _ = admission.recent_admission(str(database))
    assert first
    with closing(sqlite3.connect(database)) as conn, conn:
        conn.execute("INSERT INTO example VALUES (?)", ("synthetic" * 15000,))
    assert _enter(runner, "nurse").ok
    second, _ = admission.recent_admission(str(database))
    assert second is first
    assert runner.calls == ["full", "light"]
    handoff = json.loads(os.environ[admission.HANDOFF_ENV])
    assert handoff["check_mode"] == "recent"
    assert handoff["full_checked_at_epoch"] == first["checked_at_epoch"]
    from rem_card.data.dao.db_manager import DatabaseManager
    manager = DatabaseManager.__new__(DatabaseManager)
    matched, _ = manager._startup_guard_quickcheck_matches(handoff)
    assert matched
    assert manager._startup_quickcheck_skipped_ts == int(first["checked_at_epoch"])
    admission.invalidate_role_admission()
    os.environ[admission.HANDOFF_ENV] = json.dumps(handoff)
    assert not manager._startup_guard_quickcheck_matches(handoff)[0]


def test_expiry_is_not_extended_by_repeated_role_entries(database, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(admission, "time", SimpleNamespace(monotonic=lambda: now[0]))
    # Guard's clock must match the cache clock, without changing Python's clock.
    monkeypatch.setattr(guard, "time", SimpleNamespace(monotonic=lambda: now[0], time=time.time))
    runner = _runner()
    assert _enter(runner).ok
    now[0] = 100.0 + 16 * 60
    assert _enter(runner, "nurse").ok
    now[0] = 100.0 + admission.ADMISSION_TTL_SEC
    assert _enter(runner).ok
    assert runner.calls == ["full", "light", "full"]


@pytest.mark.parametrize("change", ["schema", "migration", "revision", "replacement", "quarantine", "profile", "recovery"])
def test_changed_database_requires_full_scan(database, change):
    from rem_card.app.unified_db_schema import SCHEMA_FASTPATH_META_KEY
    runner = _runner()
    assert _enter(runner).ok
    if change == "replacement":
        replacement = database.with_name("replacement.db")
        replacement.write_bytes(database.read_bytes())
        os.replace(replacement, database)
    elif change == "quarantine":
        folder = database.parent.parent / "quarantine" / "shared_db"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "synthetic_failure").write_text("synthetic")
    elif change == "recovery":
        (database.parent.parent / "locks" / "recovery.lock").write_text("synthetic")
    else:
        with closing(sqlite3.connect(database)) as conn, conn:
            if change == "schema":
                conn.execute("CREATE TABLE changed(value TEXT)")
            elif change == "migration":
                conn.execute("INSERT INTO schema_migrations VALUES (2)")
            elif change == "revision":
                conn.execute("INSERT INTO meta VALUES (?, 'changed')", (SCHEMA_FASTPATH_META_KEY,))
            else:
                conn.execute("PRAGMA journal_mode=WAL")
    assert _enter(runner, "nurse").ok
    assert runner.calls == ["full", "light", "full"]


def test_unavailable_probe_never_grants_entry_or_requests_recovery(database, monkeypatch):
    runner = _runner()
    assert _enter(runner).ok
    database.unlink()
    monkeypatch.setattr(guard, "_check_quick_with_retries", lambda *a, **kw: (False, "database file does not exist", False))
    monkeypatch.setattr(guard, "recover_shared_db_with_locks", lambda **kw: pytest.fail("missing DB recovery"))
    assert not _enter(runner).ok
    assert admission.recent_admission(str(database))[0] is None
    assert admission.HANDOFF_ENV not in os.environ


@pytest.mark.parametrize("failure", ["policy", "rotation", "profile", "cancel"])
def test_failure_or_cancellation_discards_previous_admission(database, monkeypatch, failure):
    runner = _runner()
    assert _enter(runner).ok
    def failed(*args, **kwargs):
        raise RuntimeError("synthetic failure")
    if failure == "policy":
        monkeypatch.setattr(guard, "_load_or_create_client_policy", failed)
    elif failure == "rotation":
        from unittest.mock import Mock
        gate = Mock()
        gate.acquire.return_value = False
        monkeypatch.setattr(guard, "FileWriteLock", lambda *a, **kw: gate)
    elif failure == "profile":
        monkeypatch.setattr(guard, "_apply_network_safe_profile_with_lock", failed)
    else:
        runner.check_cancelled = lambda: (_ for _ in ()).throw(StartupCheckAborted())
    if failure == "cancel":
        with pytest.raises(StartupCheckAborted):
            _enter(runner)
    else:
        assert not _enter(runner).ok
    assert admission.recent_admission(str(database))[0] is None
    assert admission.HANDOFF_ENV not in os.environ
    if failure in {"policy", "rotation", "cancel"}:
        assert runner.calls == ["full"]


def test_unreliable_file_identity_disables_reuse_only(database, monkeypatch):
    def unsupported(*args):
        raise ValueError("synthetic missing file identity")
    monkeypatch.setattr(admission, "file_identity", unsupported)
    runner = _runner()
    assert _enter(runner).ok
    assert _enter(runner).ok
    assert runner.calls == ["full", "full"]


def test_path_change_and_process_change_cannot_reuse(database, monkeypatch):
    runner = _runner()
    assert _enter(runner).ok
    receipt, _ = admission.recent_admission(str(database))
    receipt["pid"] = -1
    assert admission.recent_admission(str(database))[0] is None
    assert _enter(runner).ok
    assert admission.recent_admission(str(database.parent / "other.db"))[0] is None


def test_error_during_scan_cannot_republish_admission(database, monkeypatch):
    runner = _runner()
    original = guard.run_quick_check
    def scan(conn):
        result = original(conn)
        admission.invalidate_role_admission()
        return result
    monkeypatch.setattr(guard, "run_quick_check", scan)
    assert _enter(runner).ok
    assert admission.recent_admission(str(database))[0] is None
    assert admission.HANDOFF_ENV not in os.environ


@pytest.mark.parametrize("message,expected", [
    ("database disk image is malformed", "corruption"),
    ("schema incompatible", "schema_incompatible"),
    ("client_policy min_client_version", "policy_block"),
])
def test_runtime_database_failure_discards_admission(database, message, expected):
    from rem_card.services.data_service import DataService
    runner = _runner()
    assert _enter(runner).ok
    service = SimpleNamespace()
    category = DataService._handle_database_access_failure(
        service, sqlite3.DatabaseError(message), source="synthetic")
    assert category == expected
    assert admission.recent_admission(str(database))[0] is None


def test_direct_failure_discards_admission_before_report_delivery(database, monkeypatch):
    from rem_card.app import db_availability
    from rem_card.services import crash_reports
    runner = _runner()
    assert _enter(runner).ok
    def capture(*args, **kwargs):
        assert admission.recent_admission(str(database))[0] is None
        return None
    monkeypatch.setattr(crash_reports, "capture_database_failure", capture)
    monkeypatch.setattr(db_availability, "_publish_direct_central_failure", lambda event: True)
    monkeypatch.setattr(db_availability, "_incident_active", False)
    monkeypatch.setattr(db_availability, "_warning_presented_for_incident", False)
    db_availability.notify_database_unavailable(
        sqlite3.OperationalError("disk i/o error"), logger=SimpleNamespace(error=lambda *a, **kw: None),
        database_path=str(database), runtime_mode="network")
    assert admission.HANDOFF_ENV not in os.environ


@pytest.mark.parametrize("reason", ["timeout", "cancelled"])
def test_blocked_light_child_can_be_aborted_and_releases_database(database, reason):
    import multiprocessing
    conn = sqlite3.connect(database)
    conn.execute("BEGIN EXCLUSIVE")
    cancel = threading.Event()
    timer = threading.Timer(1.2, cancel.set) if reason == "cancelled" else None
    if timer:
        timer.start()
    try:
        with pytest.raises(StartupCheckAborted) as error:
            run_startup_probe(str(database), cancel, admission_mode="light", timeout=1.2 if reason == "timeout" else 20)
        assert error.value.reason == reason
        assert not [p for p in multiprocessing.active_children() if p.name == "RemCardStartupCheck"]
    finally:
        if timer:
            timer.cancel()
        conn.close()
    moved = database.with_name("after_cancel.db")
    database.rename(moved)
    with closing(sqlite3.connect(moved, timeout=0.1)) as check, check:
        check.execute("BEGIN EXCLUSIVE")


def test_real_child_full_and_light_probes_release_handles_and_do_not_write(database):
    before = database.read_bytes()
    cancel = threading.Event()
    full = run_startup_probe(str(database), cancel, admission_mode="full")
    light = run_startup_probe(str(database), cancel, admission_mode="light")
    assert full[0] and light[0]
    assert json.loads(full[1]) == json.loads(light[1])
    assert database.read_bytes() == before
    with closing(sqlite3.connect(database, timeout=0.1)) as conn, conn:
        conn.execute("BEGIN EXCLUSIVE")
    assert run_startup_probe(str(database.parent / "missing.db"), cancel, admission_mode="light")[0] is False


def test_actual_qt_role_runner_uses_child_and_keeps_timers_alive(database):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from rem_card.ui.shared.startup_check import role_startup_check_runner
    app = QApplication.instance() or QApplication([])
    preparing, ticks = [], []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start()
    try:
        runner = role_startup_check_runner(threading.Event(), preparing.append)
        assert _enter(runner).ok
        assert _enter(runner, "nurse").ok
        assert preparing == ["Проверка целостности базы…", "Проверка доступности базы…"]
        assert len(ticks) > 10
    finally:
        timer.stop()
        app.processEvents()
