from __future__ import annotations

from argparse import Namespace

import pytest

from rem_card.app import unified_main


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
