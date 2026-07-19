from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.app import main as app_main
from rem_card.app import runtime_paths
from rem_card.app.runtime_paths import DataPathConfigurationError, create_baza_structure_and_db


def test_compiled_first_run_opens_shared_path_dialog(monkeypatch):
    calls = []
    monkeypatch.setattr(app_main, "is_compiled", lambda: True)
    monkeypatch.setattr(app_main, "read_configured_baza_dir", lambda: None)
    monkeypatch.setattr(
        app_main,
        "_configure_data_path_interactively",
        lambda *, first_run: calls.append(first_run) or True,
    )
    assert app_main._ensure_compiled_data_path_configured() is True
    assert calls == [True]


def test_compiled_configured_start_skips_dialog(monkeypatch):
    monkeypatch.setattr(app_main, "is_compiled", lambda: True)
    monkeypatch.setattr(app_main, "read_configured_baza_dir", lambda: r"Z:\arbitrary-name")
    monkeypatch.setattr(
        app_main,
        "_configure_data_path_interactively",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("dialog must not open")),
    )
    assert app_main._ensure_compiled_data_path_configured() is True


def test_new_data_root_rejects_foreign_nonempty_directory_without_modifying_it(tmp_path):
    selected = tmp_path / "photos"
    selected.mkdir()
    foreign = selected / "family.jpg"
    foreign.write_bytes(b"jpeg")

    ok, message = create_baza_structure_and_db(str(selected))

    assert ok is False
    assert "не пуста" in message
    assert foreign.read_bytes() == b"jpeg"
    assert not (selected / "archiv").exists()


def test_blank_data_root_is_rejected_without_using_current_directory():
    ok, message = create_baza_structure_and_db("")
    assert ok is False
    assert "Выберите папку" in message


@pytest.mark.parametrize("payload", [[], {"baza_dir": 42}])
def test_compiled_path_config_rejects_invalid_json_shape(tmp_path, monkeypatch, payload):
    config_path = tmp_path / "remcard_data_path.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("REMCARD_DATA_PATH_CONFIG", str(config_path))

    with pytest.raises(DataPathConfigurationError):
        runtime_paths.read_configured_baza_dir()


def test_legacy_default_folder_name_is_confined_to_dev_default_constant():
    legacy_name = "Baza_" + "rao3_jurnal"
    allowed_suffixes = {".py", ".md", ".toml", ".ini", ".spec"}
    matches = []
    tracked = subprocess.check_output(
        ["git", "ls-files"],
        cwd=PROJECT_DIR,
        text=True,
        encoding="utf-8",
    ).splitlines()
    for relative in tracked:
        path = PROJECT_DIR / relative
        if path.suffix.lower() not in allowed_suffixes:
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if legacy_name in line:
                matches.append((path.relative_to(PROJECT_DIR).as_posix(), line.strip()))
    assert matches == [
        (
            "app/runtime_paths.py",
            f'DEFAULT_DEV_DATA_ROOT_NAME = "{legacy_name}"',
        )
    ]
