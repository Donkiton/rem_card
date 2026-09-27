from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import textwrap
import threading

import pytest

from rem_card.app.exit_watchdog import ExitWatchdog


PROJECT_DIR = Path(__file__).resolve().parents[1]


def test_arm_is_idempotent_and_repeated_arm_does_not_extend_deadline():
    watchdog = ExitWatchdog(timeout_sec=30, terminate=lambda _code: None)
    watchdog.arm("first")
    first_session = watchdog._session
    assert first_session is not None
    first_deadline = first_session.deadline_monotonic

    watchdog.arm("second")

    assert watchdog._session is first_session
    assert watchdog._session.deadline_monotonic == first_deadline
    assert watchdog._session.stage == "first"
    watchdog.disarm()


def test_disarm_prevents_termination_and_rearm_ignores_stale_thread():
    calls = []
    watchdog = ExitWatchdog(timeout_sec=30, terminate=calls.append)

    watchdog.arm()
    stale_session = watchdog._session
    assert stale_session is not None
    watchdog.disarm()
    assert not watchdog.armed

    watchdog.arm("rearmed")
    current_session = watchdog._session
    assert current_session is not None
    assert current_session is not stale_session
    assert watchdog.armed
    watchdog._finish(stale_session, reason="timeout")
    assert calls == []
    assert watchdog._session is current_session
    assert watchdog.armed

    watchdog.force()
    assert calls == [2]


def test_set_stage_is_written_to_diagnostic(tmp_path):
    terminated = threading.Event()
    watchdog = ExitWatchdog(
        timeout_sec=0.05,
        diagnostic_dir=tmp_path,
        terminate=lambda _code: terminated.set(),
    )
    watchdog.arm("shutdown")
    watchdog.set_stage("closing-workers")

    assert terminated.wait(0.5)
    reports = list(tmp_path.glob("exit_watchdog_*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["reason"] == "timeout"
    assert payload["pid"] == os.getpid()
    assert payload["stage"] == "closing-workers"
    assert payload["timing"]["elapsed_sec"] >= 0.05
    assert payload["timing"]["deadline_monotonic"] >= payload["timing"]["armed_monotonic"]


def test_force_terminates_once_without_prior_arm():
    calls = []
    watchdog = ExitWatchdog(timeout_sec=30, terminate=calls.append)

    watchdog.force()
    watchdog.force()

    assert calls == [2]
    assert not watchdog.armed


@pytest.mark.parametrize("diagnostic_mode", ["normal", "failing", "blocking"])
def test_default_terminator_exits_blocked_process_with_code_two(tmp_path, diagnostic_mode):
    diagnostic_path = tmp_path / diagnostic_mode
    if diagnostic_mode == "failing":
        diagnostic_path.write_text("not a directory", encoding="utf-8")

    source = textwrap.dedent(
        """
        import sys
        import threading

        from rem_card.app.exit_watchdog import ExitWatchdog

        watchdog = ExitWatchdog(timeout_sec=0.12, diagnostic_dir=sys.argv[1])
        if sys.argv[2] == "blocking":
            blocked = threading.Event()
            watchdog._write_diagnostic = lambda _payload: blocked.wait()
        watchdog.arm("subprocess-smoke")
        threading.Event().wait()
        """
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(PROJECT_DIR), env.get("PYTHONPATH", "")))
    )
    completed = subprocess.run(
        [sys.executable, "-c", source, str(diagnostic_path), diagnostic_mode],
        cwd=PROJECT_DIR,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    )

    assert completed.returncode == 2, completed.stdout + completed.stderr
    if diagnostic_mode == "normal":
        reports = list(diagnostic_path.glob("exit_watchdog_*.json"))
        assert len(reports) == 1
        assert json.loads(reports[0].read_text(encoding="utf-8"))["reason"] == "timeout"


def test_forced_exit_recovers_sqlite_and_rolls_back_uncommitted_write(tmp_path):
    database_path = tmp_path / "recovery.sqlite"
    diagnostic_path = tmp_path / "diagnostics"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        connection.execute("CREATE TABLE records (value TEXT NOT NULL)")
        connection.execute("INSERT INTO records VALUES ('confirmed')")

    source = textwrap.dedent(
        """
        import sqlite3
        import sys
        import threading

        from rem_card.app.exit_watchdog import ExitWatchdog

        connection = sqlite3.connect(sys.argv[1])
        connection.execute("INSERT INTO records VALUES ('uncommitted')")
        assert connection.in_transaction
        watchdog = ExitWatchdog(timeout_sec=0.12, diagnostic_dir=sys.argv[2])
        watchdog.arm("sqlite-transaction-open")
        threading.Event().wait()
        """
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(PROJECT_DIR), env.get("PYTHONPATH", "")))
    )
    completed = subprocess.run(
        [sys.executable, "-c", source, str(database_path), str(diagnostic_path)],
        cwd=PROJECT_DIR,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    )

    assert completed.returncode == 2, completed.stdout + completed.stderr
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert connection.execute("SELECT value FROM records").fetchall() == [
            ("confirmed",)
        ]
