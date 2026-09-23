import multiprocessing
import sqlite3
import threading
import time

import pytest

from rem_card.app.startup_check_worker import (
    StartupCheckAborted, current_startup_check_runner,
    run_startup_probe, startup_check_runner,
)


def _slow_probe(path, sender):
    # Hold a SQLite handle to prove cancellation also releases real resources.
    conn = sqlite3.connect(path)
    conn.execute("BEGIN EXCLUSIVE")
    time.sleep(10)
    sender.send((True, "ok", False))
    conn.close()


def _db(tmp_path):
    path = tmp_path / "probe.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE example(value TEXT)")
        conn.execute("INSERT INTO example VALUES ('synthetic')")
    return path


def test_probe_checks_actual_database_without_changing_it(tmp_path):
    path = _db(tmp_path)
    before = path.read_bytes()
    assert run_startup_probe(str(path), threading.Event()) == (True, "ok", False)
    assert path.read_bytes() == before


def test_unavailable_and_corruption_are_distinct(tmp_path):
    ok, _, corrupt = run_startup_probe(str(tmp_path / 'missing.db'), threading.Event())
    assert not ok and not corrupt
    path = tmp_path / 'invalid.db'
    path.write_bytes(b'not sqlite' * 100)
    ok, _, corrupt = run_startup_probe(str(path), threading.Event())
    assert not ok and corrupt
    assert path.read_bytes() == b'not sqlite' * 100


@pytest.mark.parametrize('reason', ['timeout', 'cancelled'])
def test_abort_waits_for_process_and_releases_file(tmp_path, reason):
    path = _db(tmp_path)
    cancel = threading.Event()
    timer = threading.Timer(1.2, cancel.set) if reason == 'cancelled' else None
    if timer:
        timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(StartupCheckAborted) as error:
            run_startup_probe(str(path), cancel, timeout=1.2 if reason == 'timeout' else 20,
                              _target=_slow_probe)
        assert error.value.reason == reason
        assert time.monotonic() - started < 5
        assert not [p for p in multiprocessing.active_children() if p.name == 'RemCardStartupCheck']
        with sqlite3.connect(path, timeout=0.1) as conn:
            conn.execute("BEGIN EXCLUSIVE")
            conn.execute("INSERT INTO example VALUES ('after abort')")
    finally:
        if timer:
            timer.cancel()


def test_runner_scope_restores_and_cancellation_is_not_recovery(tmp_path, monkeypatch):
    from rem_card.app import startup_db_guard as guard
    assert current_startup_check_runner() is None
    def cancelled(path):
        raise StartupCheckAborted()
    with startup_check_runner(cancelled):
        with pytest.raises(StartupCheckAborted):
            guard._check_quick_with_retries(str(tmp_path / 'db'), baza_dir=str(tmp_path), role='doctor')
    assert current_startup_check_runner() is None


def test_responsive_scan_keeps_qt_timer_alive_and_waits_for_cleanup():
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from rem_card.ui.shared.startup_check import responsive_startup_probe
    app = QApplication.instance() or QApplication([])
    cancel = threading.Event()
    ticks = []
    cleaned = []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start()
    def probe(path, event):
        assert event.wait(2)
        time.sleep(0.08)
        cleaned.append(True)
        raise StartupCheckAborted()
    QTimer.singleShot(100, cancel.set)
    try:
        with pytest.raises(StartupCheckAborted):
            responsive_startup_probe('unused', cancel, probe=probe)
        assert cleaned and len(ticks) >= 5
    finally:
        timer.stop()
        app.processEvents()


def test_responsive_scan_propagates_attempt_context_and_clears_it():
    from PySide6.QtWidgets import QApplication
    from rem_card.app.startup_diagnostics import startup_attempt, startup_context
    from rem_card.ui.shared.startup_check import responsive_startup_probe
    app = QApplication.instance() or QApplication([])
    captured = []
    def probe(path, event):
        captured.append(startup_context())
        return True, 'ok', False
    with startup_attempt('synthetic-session', 'doctor'):
        assert responsive_startup_probe('unused', threading.Event(), probe=probe)[0]
    assert captured == [{'session_id': 'synthetic-session', 'role': 'doctor'}]
    assert startup_context() == {}
    app.processEvents()


@pytest.mark.parametrize('result', [(True, 'ok', False), (False, 'malformed', True)])
def test_cancel_after_scan_prevents_profile_or_recovery(tmp_path, monkeypatch, result):
    from rem_card.app import startup_db_guard as guard
    cancel = threading.Event()
    monkeypatch.setattr(guard, 'resolve_baza_dir', lambda: str(tmp_path))
    monkeypatch.setattr(guard, '_load_or_create_client_policy', lambda *args: None)
    monkeypatch.setattr(guard, 'write_audit_event', lambda *args, **kwargs: None)
    monkeypatch.setattr(guard, '_apply_network_safe_profile_with_lock', lambda **kwargs: pytest.fail('profile after cancel'))
    monkeypatch.setattr(guard, 'recover_shared_db_with_locks', lambda **kwargs: pytest.fail('recovery after cancel'))
    def runner(path):
        cancel.set()
        return result
    def check():
        if cancel.is_set():
            raise StartupCheckAborted()
    runner.check_cancelled = check
    with startup_check_runner(runner), pytest.raises(StartupCheckAborted):
        guard.run_startup_db_guard(role='doctor')
