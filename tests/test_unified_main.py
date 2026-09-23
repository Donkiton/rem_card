from __future__ import annotations

from argparse import Namespace

import pytest

from rem_card.app import unified_main


def test_unified_session_saves_real_thread_dump_and_session_id(tmp_path, monkeypatch):
    import json
    from rem_card.services import crash_reports

    monkeypatch.setenv("REMCARD_CRASH_OUTBOX_DIR", str(tmp_path))
    with unified_main._crash_session():
        session_id = crash_reports.current_crash_session_id()
        assert session_id
        assert crash_reports.dump_current_thread_stacks(reason="synthetic_ui_hang")
        assert session_id in next((tmp_path / "sessions").glob("*.json")).read_text()
    assert not list((tmp_path / "sessions").glob("*.json"))
    assert crash_reports.current_crash_session_id() == ""
    report = json.loads(next((tmp_path / "outbox").glob("*.json")).read_text(encoding="utf-8"))
    assert report["session_id"] == session_id
    assert report["event_type"] == "ui_hang"


def test_unified_session_exception_finalizes_with_original_session(tmp_path, monkeypatch):
    import json
    from rem_card.services import crash_reports

    monkeypatch.setenv("REMCARD_CRASH_OUTBOX_DIR", str(tmp_path))
    with pytest.raises(RuntimeError):
        with unified_main._crash_session():
            session_id = crash_reports.current_crash_session_id()
            raise RuntimeError("synthetic initialization failure")
    reports = list((tmp_path / "outbox").glob("*.json"))
    assert len(reports) == 1
    assert json.loads(reports[0].read_text(encoding="utf-8"))["session_id"] == session_id
    assert not list((tmp_path / "sessions").glob("*.json"))


def test_top_level_failure_writes_exactly_one_report_with_session(tmp_path):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    env = os.environ.copy()
    env["REMCARD_CRASH_OUTBOX_DIR"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "-c", "from rem_card.app.unified_main import _crash_session\n"
         "with _crash_session():\n    raise RuntimeError('synthetic-top-level-failure')"],
        cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True, timeout=20,
    )
    assert result.returncode != 0
    reports = list((tmp_path / "outbox").glob("*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["session_id"] and payload["event_type"] == "unhandled_python_exception"


@pytest.mark.parametrize("acquired", [True, False])
def test_main_initializes_diagnostics_before_window_only_for_primary_instance(tmp_path, monkeypatch, acquired):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from PySide6 import QtCore, QtWidgets
    from PySide6.QtNetwork import QLocalServer
    from rem_card.app import main, unified_preflight
    from rem_card.ui import unified_window
    from rem_card.services import crash_reports

    monkeypatch.setenv("REMCARD_CRASH_OUTBOX_DIR", str(tmp_path))
    monkeypatch.setattr(unified_main.multiprocessing, "freeze_support", lambda: None)
    app = Mock()
    app.exec.return_value = 0
    monkeypatch.setattr(QtWidgets.QApplication, "instance", lambda: app)
    monkeypatch.setattr(QtCore.QTimer, "singleShot", lambda *args: None)
    monkeypatch.setattr(QLocalServer, "removeServer", lambda *args: True)
    server = Mock()
    monkeypatch.setattr(main, "_prepare_single_instance_server",
                        lambda *a: (server, acquired, main.SINGLE_INSTANCE_ACQUIRED if acquired else "shown"))
    monkeypatch.setattr(main, "_connect_single_instance_requests", lambda *a: None)
    monkeypatch.setattr(unified_preflight, "attach_startup_request", lambda *a: None)
    monkeypatch.setattr(unified_preflight, "build_startup_request", lambda **kw: None)
    created = []

    def window():
        assert crash_reports.current_crash_session_id()
        created.append(True)
        return SimpleNamespace(show=lambda: None, initialize=lambda: None)

    monkeypatch.setattr(unified_window, "UnifiedWindow", window)
    unified_main.main([])
    assert created == ([True] if acquired else [])
    assert app.exec.call_count == int(acquired)
    assert not crash_reports.current_crash_session_id()
    assert not list((tmp_path / "sessions").glob("*.json"))


def test_frozen_worker_dispatch_precedes_application_argument_parsing(monkeypatch, capsys):
    dispatched = []

    monkeypatch.setattr(unified_main.multiprocessing, "freeze_support", lambda: dispatched.append(True))

    def parse(_argv):
        assert dispatched == [True]
        return Namespace(
            restart_after_pid=0,
            role=None,
            emergency_startup_request="",
            compiled_smoke=True,
        )

    monkeypatch.setattr(unified_main, "_parse_args", parse)
    monkeypatch.setattr(unified_main, "_run_compiled_worker_smoke", lambda: True)
    unified_main.main([])

    assert "REMCARD_UNIFIED_SMOKE_OK" in capsys.readouterr().out


def test_compiled_smoke_starts_real_spawn_worker():
    assert unified_main._run_compiled_worker_smoke(timeout_seconds=20.0)


def test_compiled_smoke_is_safe_without_console_streams(monkeypatch):
    monkeypatch.setattr(unified_main.multiprocessing, "freeze_support", lambda: None)
    monkeypatch.setattr(unified_main, "_run_compiled_worker_smoke", lambda: True)
    monkeypatch.setattr(unified_main.sys, "stdout", None)
    monkeypatch.setattr(unified_main.sys, "stderr", None)

    unified_main.main(["--compiled-smoke"])


def test_compiled_smoke_fails_closed_when_worker_does_not_start(monkeypatch):
    monkeypatch.setattr(unified_main.multiprocessing, "freeze_support", lambda: None)
    monkeypatch.setattr(unified_main, "_run_compiled_worker_smoke", lambda: False)

    with pytest.raises(SystemExit) as exc_info:
        unified_main.main(["--compiled-smoke"])

    assert exc_info.value.code == 3


def test_invalid_gui_arguments_exit_cleanly_without_stderr(monkeypatch):
    messages = []
    monkeypatch.setattr(unified_main.sys, "stderr", None)
    monkeypatch.setattr(unified_main, "_show_argument_error", messages.append)

    with pytest.raises(SystemExit) as exc_info:
        unified_main._parse_args(["--unknown-worker-argument"])

    assert exc_info.value.code == 2
    assert messages and "unrecognized arguments" in messages[0]


def test_emergency_request_keeps_legacy_role_validation():
    with pytest.raises(SystemExit) as exc_info:
        unified_main._parse_args(["--role", "settings", "--emergency-startup-request", "request.json"])

    assert exc_info.value.code == 2


def test_resume_role_is_separate_from_legacy_hint():
    args = unified_main._parse_args(['--resume-role', 'nurse'])
    assert args.resume_role == 'nurse' and args.role is None


def test_compiled_restart_passes_explicit_continuation(monkeypatch, tmp_path):
    from rem_card.app import main, process_launch
    calls = []
    monkeypatch.setattr(main, 'is_compiled', lambda: True)
    monkeypatch.setattr(main.sys, 'argv', [str(tmp_path / 'RemCard.exe')])
    monkeypatch.setattr(process_launch, 'popen_hidden', lambda command, **kw: calls.append(command))
    assert main._launch_requested_restart(resume_role='nurse')
    assert calls[0][-2:] == ['--resume-role', 'nurse']


def test_dev_restart_does_not_replay_previous_emergency_role(monkeypatch, tmp_path):
    from rem_card.app import main, process_launch
    calls = []
    monkeypatch.setattr(main, 'is_compiled', lambda: False)
    monkeypatch.setattr(main.sys, 'argv', [str(tmp_path / 'run_remcard.py'), '--resume-role', 'nurse',
        '--emergency-startup-request=request.json', '--restart-after-pid', '123'])
    monkeypatch.setattr(process_launch, 'popen_hidden', lambda command, **kw: calls.append(command))
    assert main._launch_requested_restart()
    assert '--resume-role' not in calls[0]
    assert not any('request.json' in value for value in calls[0])
    assert calls[0].count('--restart-after-pid') == 1
