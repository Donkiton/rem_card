from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from pathlib import Path
from types import SimpleNamespace

from rem_card.app.isolated_test_runtime import user_profile_environment


PROJECT_DIR = Path(__file__).resolve().parents[1]


def _isolated_environment(tmp_path: Path) -> tuple[dict[str, str], Path]:
    environment = dict(os.environ)
    for key in list(environment):
        if key.startswith("REMCARD_"):
            environment.pop(key, None)
    profile = tmp_path / "profile"
    profile.mkdir()
    environment.update(user_profile_environment(profile))
    for key in ("LOCALAPPDATA", "APPDATA", "ProgramData", "TEMP", "TMP"):
        directory = tmp_path / key.lower()
        directory.mkdir()
        environment[key] = str(directory)
    config_path = tmp_path / "data_path.json"
    environment.update(
        {
            "PYTHONUTF8": "1",
            "QT_QPA_PLATFORM": "offscreen",
            "REMCARD_DATA_PATH_CONFIG": str(config_path),
            "REMCARD_EMERGENCY_DB_ROOT": str(tmp_path / "emergency"),
            "REMCARD_LOCAL_LOGS_DIR": str(tmp_path / "logs"),
            "REMCARD_FULL_RUNTIME_THEME": "0",
            "REMCARD_STARTUP_W1_WAIT_MS": "0",
            "REMCARD_TEST_INSTANCE_NAMESPACE": str(tmp_path),
        }
    )
    return environment, config_path


@pytest.mark.parametrize("qt_platform", ["offscreen", pytest.param(
    "windows", marks=pytest.mark.skipif(sys.platform != "win32", reason="Windows Qt integration")
)])
def test_first_run_empty_folder_connects_immediately_and_reopens(tmp_path, qt_platform):
    environment, config_path = _isolated_environment(tmp_path)
    selected = tmp_path / "new-central-root"
    selected.mkdir()
    environment["REMCARD_FIRST_RUN_TEST_ROOT"] = str(selected)
    environment["QT_QPA_PLATFORM"] = qt_platform

    result = subprocess.run(
        [sys.executable, str(PROJECT_DIR / "tests" / "unified_first_run_smoke.py")],
        cwd=PROJECT_DIR,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )

    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "UNIFIED_FIRST_RUN_DOCTOR_OK" in result.stdout
    assert "UNIFIED_INSTITUTION_SAVE_OK" in result.stdout
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert Path(payload["baza_dir"]) == selected.absolute()
    assert (selected / "archiv" / "rao_journal.db").is_file()
    assert (selected / "settings" / "remcard_settings.db").is_file()


@pytest.fixture
def first_run_shell(tmp_path, monkeypatch):
    environment, config_path = _isolated_environment(tmp_path)
    for key, value in environment.items():
        if key.startswith("REMCARD_"):
            monkeypatch.setenv(key, value)

    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from rem_card.app import local_administrator, runtime_paths
    from rem_card.ui import unified_window

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(local_administrator, "is_local_administrator", lambda: False)
    monkeypatch.setattr(runtime_paths, "is_compiled", lambda: True)
    monkeypatch.setattr(runtime_paths, "read_configured_baza_dir", lambda: None)
    monkeypatch.setattr(unified_window, "QSettings", lambda *_: QSettings(
        str(tmp_path / "shell.ini"), QSettings.IniFormat,
    ))
    shell = unified_window.UnifiedWindow()
    monkeypatch.setattr(shell, "_dispatch_startup_role", lambda: None)
    monkeypatch.setattr(shell, "_check_updates", lambda: None)
    shell.initialize()
    assert shell.root == "" and shell.store is None
    assert shell.stack.currentWidget() is shell.welcome
    yield shell, config_path
    shell._exit_update_checked = True
    shell._candidate = None
    shell._busy = shell._leaving = False
    shell._entry_cancel = None
    shell.close()
    shell.deleteLater()
    app.processEvents()


@pytest.fixture
def database_dialog(monkeypatch):
    from PySide6.QtCore import QTimer
    from rem_card.ui.shared import unified_settings_dialogs

    responses, opened = [], []

    class Dialog(unified_settings_dialogs.DatabasePathDialog):
        def exec(self):
            opened.append(self)
            path = responses.pop(0) if responses else None
            if path is not None:
                self.path_edit.setText(str(path))
            QTimer.singleShot(0, self.accept if path is not None else self.reject)
            return super().exec()

    monkeypatch.setattr(unified_settings_dialogs, "DatabasePathDialog", Dialog)
    return SimpleNamespace(responses=responses, opened=opened)


def _synchronous_calls(shell):
    def synchronous(fn, done, failed=None):
        try:
            done(fn())
        except Exception as exc:
            (failed or shell._error)(exc)
    return synchronous


@pytest.mark.parametrize("role", ["doctor", "nurse", "settings"])
def test_first_run_selection_opens_database_dialog_and_cancel_allows_retry(
    first_run_shell, database_dialog, role,
):
    shell, config_path = first_run_shell
    assert all(card.isEnabled() for card in shell.welcome.role_buttons.values())
    assert not database_dialog.opened
    for attempt in range(2):
        if role == "settings":
            shell.open_settings()
        else:
            shell.welcome.role_buttons[role].click()
        assert len(database_dialog.opened) == attempt + 1
        assert all(card.isEnabled() for card in shell.welcome.role_buttons.values())
        assert shell.root == "" and not config_path.exists()
        assert shell.container is None and not shell._busy


@pytest.mark.parametrize("role", ["operblock_planned", "operblock_emergency"])
def test_first_run_operblock_selection_never_requests_central_configuration(
    first_run_shell, database_dialog, monkeypatch, role,
):
    shell, config_path = first_run_shell
    admissions = []
    monkeypatch.setattr(shell, "_start_local_operblock_admission", admissions.append)
    shell.welcome.role_buttons[role].click()
    assert admissions == [shell.session_id]
    assert shell.role == role and shell._busy
    assert not database_dialog.opened and not config_path.exists()
    assert not any(card.isEnabled() for card in shell.welcome.role_buttons.values())


@pytest.mark.parametrize("role", ["doctor", "nurse"])
def test_first_run_invalid_nonempty_folder_is_not_saved_and_allows_retry(
    tmp_path, monkeypatch, first_run_shell, database_dialog, role,
):
    shell, config_path = first_run_shell
    selected = tmp_path / "foreign-folder"
    selected.mkdir()
    (selected / "notes.txt").write_text("user data", encoding="utf-8")
    database_dialog.responses.append(selected)
    monkeypatch.setattr(shell, "_async", _synchronous_calls(shell))
    shell.welcome.role_buttons[role].click()

    assert "не найдена база данных" in shell.welcome.access_label.text()
    assert shell.root == "" and shell.container is None
    assert not config_path.exists()
    assert (selected / "notes.txt").read_text(encoding="utf-8") == "user data"
    assert list(selected.iterdir()) == [selected / "notes.txt"]
    assert all(card.isEnabled() for card in shell.welcome.role_buttons.values())
    shell.welcome.role_buttons[role].click()
    assert len(database_dialog.opened) == 2


@pytest.mark.parametrize("role", ["doctor", "nurse"])
def test_first_run_incompatible_candidate_allows_choosing_another_database(
    tmp_path, monkeypatch, first_run_shell, database_dialog, role,
):
    from rem_card.app import unified_database_setup
    from rem_card.app.unified_runtime import CompatibilityError
    from rem_card.ui import unified_window

    shell, config_path = first_run_shell
    database_dialog.responses.append(tmp_path)
    monkeypatch.setattr(shell, "_async", _synchronous_calls(shell))
    monkeypatch.setattr(unified_database_setup, "prepare_database_root", lambda _: (True, "existing"))

    def incompatible(*_):
        raise CompatibilityError("Требуется обновление RemCard")

    monkeypatch.setattr(unified_window, "check_client_compatibility", incompatible)
    monkeypatch.setattr(shell, "_check_updates", lambda: pytest.fail("Candidate is not the configured database"))
    shell.welcome.role_buttons[role].click()
    assert "Требуется обновление" in shell.welcome.access_label.text()
    assert not shell._compatibility_error
    assert shell.root == "" and not config_path.exists()
    assert all(card.isEnabled() for card in shell.welcome.role_buttons.values())
    shell.welcome.role_buttons[role].click()
    assert len(database_dialog.opened) == 2


@pytest.mark.parametrize("role", ["doctor", "nurse"])
def test_first_run_local_operblock_exit_preserves_setup_and_restart_boundary(
    tmp_path, monkeypatch, first_run_shell, database_dialog, role,
):
    from rem_card.app import unified_database_setup
    from rem_card.ui import unified_window

    shell, config_path = first_run_shell
    shell.role = "operblock_planned"
    shell._local_only = shell._local_operblock = True
    shell._local_only_runtime_state = {"fresh_runtime_required_after_drain": True}
    shell._shutdown = SimpleNamespace(containers=[])
    monkeypatch.setattr(shell, "_request_opblock_sync", lambda: None)
    shell._finish_drained()
    assert shell._requires_fresh_runtime and not shell._local_only
    assert all(card.isEnabled() for card in shell.welcome.role_buttons.values())

    shell.welcome.role_buttons[role].click()
    assert len(database_dialog.opened) == 1
    assert not shell._restart and not config_path.exists()

    database_dialog.responses.append(tmp_path)
    monkeypatch.setattr(shell, "_async", _synchronous_calls(shell))
    monkeypatch.setattr(unified_database_setup, "prepare_database_root", lambda _: (True, "existing"))
    monkeypatch.setattr(unified_window, "check_client_compatibility", lambda *_: None)
    monkeypatch.setattr(shell, "_configure_store", lambda: pytest.fail("Central bootstrap requires restart"))
    closed = []
    monkeypatch.setattr(shell, "close", lambda: closed.append(True))
    shell.welcome.role_buttons[role].click()
    assert len(database_dialog.opened) == 2 and closed == [True]
    assert shell._restart and shell._suppress_exit_update
    assert shell._restart_resume_role is None
    assert shell.container is None
    assert Path(json.loads(config_path.read_text(encoding="utf-8"))["baza_dir"]) == tmp_path
