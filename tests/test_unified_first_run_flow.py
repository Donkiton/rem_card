from __future__ import annotations

import json
import os
import subprocess
import sys
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


def test_first_run_empty_folder_is_saved_reopened_and_bootstraps_doctor(tmp_path):
    environment, config_path = _isolated_environment(tmp_path)
    selected = tmp_path / "new-central-root"
    selected.mkdir()
    environment["REMCARD_FIRST_RUN_TEST_ROOT"] = str(selected)

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
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert Path(payload["baza_dir"]) == selected.absolute()
    assert (selected / "archiv" / "rao_journal.db").is_file()
    assert (selected / "settings" / "remcard_settings.db").is_file()


def test_first_run_invalid_nonempty_folder_is_not_saved(tmp_path, monkeypatch):
    environment, config_path = _isolated_environment(tmp_path)
    for key, value in environment.items():
        if key.startswith("REMCARD_"):
            monkeypatch.setenv(key, value)
    selected = tmp_path / "foreign-folder"
    selected.mkdir()
    (selected / "notes.txt").write_text("user data", encoding="utf-8")

    from rem_card.app import runtime_paths
    from rem_card.ui.shared import unified_settings_dialogs
    from rem_card.ui.unified_window import UnifiedWindow

    class Dialog:
        def __init__(self, *_args, **_kwargs):
            self.path_edit = SimpleNamespace(text=lambda: str(selected))

        def exec(self):
            return 1

    errors = []
    shell = SimpleNamespace(
        _local_only=False,
        _requires_fresh_runtime=False,
        root="",
        _error=errors.append,
    )

    def synchronous(fn, done, failed=None):
        try:
            done(fn())
        except Exception as exc:
            (failed or shell._error)(exc)

    shell._async = synchronous
    monkeypatch.setattr(runtime_paths, "is_compiled", lambda: True)
    monkeypatch.setattr(unified_settings_dialogs, "DatabasePathDialog", Dialog)

    UnifiedWindow.change_database(shell, first_run=True)

    assert errors and "не найдена база данных" in str(errors[0])
    assert not config_path.exists()
    assert (selected / "notes.txt").read_text(encoding="utf-8") == "user data"
    assert list(selected.iterdir()) == [selected / "notes.txt"]
