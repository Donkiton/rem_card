from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from rem_card.app.isolated_test_runtime import user_profile_environment
from rem_card.app.runtime_paths import get_journal_db_path, get_required_baza_paths, validate_dev_baza_dir
from rem_card.app.unified_access import MaintenanceStore
from rem_card.app.unified_database_setup import prepare_database_root
from rem_card.app.unified_db_schema import is_unified_schema_ready
from rem_card.data.settings import settings_schema


@pytest.fixture
def isolated_profile(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    for key, value in user_profile_environment(profile).items():
        monkeypatch.setenv(key, value)
    for key in ("LOCALAPPDATA", "APPDATA", "ProgramData", "TEMP", "TMP"):
        directory = tmp_path / key.lower()
        directory.mkdir()
        monkeypatch.setenv(key, str(directory))
    logs = tmp_path / "logs"
    monkeypatch.setenv("REMCARD_LOCAL_LOGS_DIR", str(logs))
    monkeypatch.setenv("REMCARD_DATA_PATH_CONFIG", str(tmp_path / "data_path.json"))
    monkeypatch.setenv("REMCARD_EMERGENCY_DB_ROOT", str(tmp_path / "emergency"))
    return tmp_path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_empty_selected_folder_creates_complete_supported_database(isolated_profile):
    root = isolated_profile / "fresh-root"
    root.mkdir()

    ok, message = prepare_database_root(root)

    assert (ok, message) == (True, "created")
    assert all(Path(path).is_dir() for path in get_required_baza_paths(str(root)))
    valid, reason = validate_dev_baza_dir(str(root))
    assert (valid, reason) == (True, "ok")

    journal_path = Path(get_journal_db_path(str(root)))
    with sqlite3.connect(journal_path) as connection:
        assert is_unified_schema_ready(connection)
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"

    settings_path = root / "settings" / "remcard_settings.db"
    with sqlite3.connect(settings_path) as connection:
        status = settings_schema.inspect_schema_status(connection)
        assert status.fastpath_ready
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"

    policy = json.loads((root / "config" / "client_policy.json").read_text(encoding="utf-8"))
    assert isinstance(policy.get("min_client_version"), str)
    assert not Path(str(journal_path) + "-wal").exists()
    assert not Path(str(settings_path) + "-wal").exists()
    assert not (root / "session_locks" / "database_setup.lock").exists()


def test_existing_valid_root_is_attached_without_reinitializing_databases(isolated_profile):
    root = isolated_profile / "existing-root"
    root.mkdir()
    assert prepare_database_root(root)[0]
    journal_path = Path(get_journal_db_path(str(root)))
    settings_path = root / "settings" / "remcard_settings.db"
    before = {_path: (_digest(_path), _path.stat().st_mtime_ns) for _path in (journal_path, settings_path)}

    assert prepare_database_root(root) == (True, "existing")

    after = {_path: (_digest(_path), _path.stat().st_mtime_ns) for _path in (journal_path, settings_path)}
    assert after == before


def test_nonempty_invalid_folder_is_rejected_without_modification(isolated_profile):
    root = isolated_profile / "foreign-root"
    root.mkdir()
    foreign = root / "family.jpg"
    sidecar = root / "notes.json"
    foreign.write_bytes(b"jpeg-data")
    sidecar.write_text('{"owner": "user"}', encoding="utf-8")
    before = {path.name: path.read_bytes() for path in root.iterdir()}

    ok, message = prepare_database_root(root)

    assert not ok
    assert "не найдена база данных" in message
    assert {path.name: path.read_bytes() for path in root.iterdir()} == before


def test_existing_root_respects_active_maintenance(isolated_profile):
    root = isolated_profile / "maintenance-root"
    root.mkdir()
    assert prepare_database_root(root)[0]
    store = MaintenanceStore(root)
    state = store.begin("test")
    try:
        assert prepare_database_root(root) == (False, "Выбранная база закрыта на обслуживание.")
    finally:
        store.cancel(
            expected_generation=state["generation"],
            operation_id=state["operation_id"],
            owner_token=state["owner_token"],
        )
