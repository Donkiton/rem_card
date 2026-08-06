from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from _local_rem_card_bootstrap import bootstrap_local_rem_card

    bootstrap_local_rem_card()
except ImportError:
    pass

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from rem_card.app import main as app_main  # noqa: E402
from rem_card.app import runtime_paths  # noqa: E402
from rem_card.data.dao.db_manager import DatabaseManager  # noqa: E402
from rem_card.data.settings.settings_db import SettingsDatabase, SettingsDbError  # noqa: E402
from rem_card.ui.admin_view.admin_main_widget import AdminMainWidget  # noqa: E402
from rem_card.ui.admin_view import dev_database_switch_dialog as switch_dialog_module  # noqa: E402
from rem_card.ui.admin_view.dev_database_switch_dialog import DevDatabaseSwitchDialog  # noqa: E402
from rem_card.ui.shared.custom_message_box import CustomMessageBox  # noqa: E402


def application() -> QApplication:
    return QApplication.instance() or QApplication([])


def normalized(path: str | os.PathLike[str]) -> str:
    return os.path.abspath(os.path.normpath(os.fspath(path)))


def isolate_dev_database_config(monkeypatch, tmp_path: Path) -> Path:
    config_path = tmp_path / "dev_database_paths.json"
    monkeypatch.setenv(runtime_paths.DEV_DATABASE_CONFIG_ENV, str(config_path))
    monkeypatch.delenv(runtime_paths.DEV_BAZA_DIR_ENV, raising=False)
    return config_path


def create_baza(root: Path) -> Path:
    for directory in runtime_paths.get_required_baza_paths(str(root)):
        Path(directory).mkdir(parents=True, exist_ok=True)
    db_path = root / "archiv" / "rao_journal.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE patients (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE admissions (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE beds (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    settings_path = root / "settings" / "remcard_settings.db"
    conn = sqlite3.connect(settings_path)
    try:
        conn.execute("CREATE TABLE settings_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
    finally:
        conn.close()
    return root


def wait_for_dialog_validation(app: QApplication, dialog: DevDatabaseSwitchDialog) -> None:
    deadline = time.monotonic() + 5.0
    while dialog._validation_worker is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    assert dialog._validation_worker is None


def test_dev_database_config_persists_normalized_deduplicated_paths(monkeypatch, tmp_path):
    config_path = isolate_dev_database_config(monkeypatch, tmp_path)
    active = tmp_path / "active_database"
    saved = tmp_path / "saved_database"
    active.mkdir()
    saved.mkdir()

    active_variant = os.path.join(str(active), "child", "..")
    saved_variant = os.path.join(str(saved), ".")
    written_path = runtime_paths.write_dev_database_config(
        active_variant,
        [
            str(active),
            active_variant,
            str(saved),
            saved_variant,
            "",
        ],
    )

    assert normalized(written_path) == normalized(config_path)
    assert config_path.is_file()
    assert runtime_paths.read_dev_database_config() == {
        "active_baza_dir": normalized(active),
        "saved_baza_dirs": [normalized(active), normalized(saved)],
    }


def test_first_saved_path_keeps_active_unset(monkeypatch, tmp_path):
    isolate_dev_database_config(monkeypatch, tmp_path)
    project_root = tmp_path / "project"
    candidate = tmp_path / "network_database"
    monkeypatch.setattr(runtime_paths, "get_project_root", lambda: str(project_root))

    runtime_paths.add_saved_dev_baza_dir(str(candidate))

    assert runtime_paths.read_dev_database_config() == {
        "active_baza_dir": None,
        "saved_baza_dirs": [normalized(candidate)],
    }
    assert runtime_paths.get_dev_baza_dir() == normalized(
        project_root / runtime_paths.DEFAULT_DEV_DATA_ROOT_NAME
    )

    runtime_paths.remove_saved_dev_baza_dir(str(candidate))
    assert runtime_paths.read_dev_database_config() == {
        "active_baza_dir": None,
        "saved_baza_dirs": [],
    }


def test_default_dev_database_config_is_scoped_to_checkout(monkeypatch, tmp_path):
    monkeypatch.delenv(runtime_paths.DEV_DATABASE_CONFIG_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local_appdata"))
    checkout = {"root": tmp_path / "checkout_a"}
    checkout["root"].mkdir()
    monkeypatch.setattr(
        runtime_paths,
        "get_dev_checkout_root",
        lambda: str(checkout["root"]),
    )

    first_path = Path(runtime_paths.get_dev_database_config_path())
    assert first_path == checkout["root"] / ".remcard" / "dev_database_paths.json"
    runtime_paths.write_dev_database_config(str(tmp_path / "baza_a"), [])

    checkout["root"] = tmp_path / "checkout_b"
    checkout["root"].mkdir()
    second_path = Path(runtime_paths.get_dev_database_config_path())
    assert second_path == checkout["root"] / ".remcard" / "dev_database_paths.json"
    assert second_path != first_path
    assert runtime_paths.read_dev_database_config() == {
        "active_baza_dir": None,
        "saved_baza_dirs": [],
    }
    runtime_paths.write_dev_database_config(str(tmp_path / "baza_b"), [])

    checkout["root"] = tmp_path / "checkout_a"
    assert runtime_paths.read_dev_database_config()["active_baza_dir"] == normalized(
        tmp_path / "baza_a"
    )


def test_legacy_global_dev_config_is_imported_only_once(monkeypatch, tmp_path):
    monkeypatch.delenv(runtime_paths.DEV_DATABASE_CONFIG_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local_appdata"))
    checkout_root = tmp_path / "checkout"
    checkout_root.mkdir()
    monkeypatch.setattr(runtime_paths, "get_dev_checkout_root", lambda: str(checkout_root))
    legacy_path = Path(runtime_paths.get_legacy_dev_database_config_path())
    legacy_path.parent.mkdir(parents=True)
    legacy_active = tmp_path / "legacy_active"
    legacy_payload = {
        "version": 1,
        "active_baza_dir": str(legacy_active),
        "saved_baza_dirs": [str(legacy_active)],
    }
    legacy_path.write_text(json.dumps(legacy_payload), encoding="utf-8")

    assert runtime_paths.read_dev_database_config()["active_baza_dir"] == normalized(
        legacy_active
    )
    scoped_path = Path(runtime_paths.get_dev_database_config_path())
    marker_path = Path(runtime_paths.get_dev_database_migration_marker_path())
    assert scoped_path.is_file()
    assert marker_path.is_file()
    assert json.loads(legacy_path.read_text(encoding="utf-8")) == legacy_payload

    scoped_path.unlink()
    replacement = tmp_path / "changed_legacy_active"
    legacy_payload["active_baza_dir"] = str(replacement)
    legacy_path.write_text(json.dumps(legacy_payload), encoding="utf-8")

    assert runtime_paths.read_dev_database_config() == {
        "active_baza_dir": None,
        "saved_baza_dirs": [],
    }
    assert not scoped_path.exists()


def test_explicit_dev_config_override_does_not_import_legacy(monkeypatch, tmp_path):
    override_path = isolate_dev_database_config(monkeypatch, tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local_appdata"))
    legacy_path = Path(runtime_paths.get_legacy_dev_database_config_path())
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        json.dumps(
            {
                "active_baza_dir": str(tmp_path / "legacy"),
                "saved_baza_dirs": [str(tmp_path / "legacy")],
            }
        ),
        encoding="utf-8",
    )

    assert runtime_paths.read_dev_database_config() == {
        "active_baza_dir": None,
        "saved_baza_dirs": [],
    }
    assert not override_path.exists()


def test_save_and_remove_dev_baza_dir_update_persisted_selection(monkeypatch, tmp_path):
    isolate_dev_database_config(monkeypatch, tmp_path)
    first = tmp_path / "first_database"
    second = tmp_path / "second_database"
    first.mkdir()
    second.mkdir()

    runtime_paths.write_dev_database_config(str(first), [str(first)])
    runtime_paths.save_dev_baza_dir(os.path.join(str(second), "."))

    assert runtime_paths.read_dev_database_config() == {
        "active_baza_dir": normalized(second),
        "saved_baza_dirs": [normalized(second), normalized(first)],
    }

    runtime_paths.remove_saved_dev_baza_dir(os.path.join(str(first), "child", ".."))

    assert runtime_paths.read_dev_database_config() == {
        "active_baza_dir": normalized(second),
        "saved_baza_dirs": [normalized(second)],
    }


def test_get_dev_baza_dir_prefers_environment_override_then_saved_config(monkeypatch, tmp_path):
    isolate_dev_database_config(monkeypatch, tmp_path)
    configured = tmp_path / "configured_database"
    overridden = tmp_path / "environment_database"

    runtime_paths.write_dev_database_config(str(configured), [str(configured)])
    assert runtime_paths.get_dev_baza_dir() == normalized(configured)

    monkeypatch.setenv(runtime_paths.DEV_BAZA_DIR_ENV, os.path.join(str(overridden), "."))
    assert runtime_paths.get_dev_baza_dir() == normalized(overridden)

    monkeypatch.delenv(runtime_paths.DEV_BAZA_DIR_ENV)
    assert runtime_paths.get_dev_baza_dir() == normalized(configured)


def test_get_dev_baza_dir_falls_back_to_project_database(monkeypatch, tmp_path):
    isolate_dev_database_config(monkeypatch, tmp_path)
    project_root = tmp_path / "project"
    monkeypatch.setattr(runtime_paths, "get_project_root", lambda: str(project_root))

    assert runtime_paths.get_dev_baza_dir() == normalized(
        project_root / runtime_paths.DEFAULT_DEV_DATA_ROOT_NAME
    )


def test_broken_dev_database_config_is_quarantined_and_falls_back(monkeypatch, tmp_path):
    config_path = isolate_dev_database_config(monkeypatch, tmp_path)
    project_root = tmp_path / "project"
    monkeypatch.setattr(runtime_paths, "get_project_root", lambda: str(project_root))
    config_path.write_text("{broken json", encoding="utf-8")

    assert runtime_paths.get_dev_baza_dir() == normalized(
        project_root / runtime_paths.DEFAULT_DEV_DATA_ROOT_NAME
    )
    assert not config_path.exists()
    assert list(tmp_path.glob("dev_database_paths.json.broken.*"))


def test_dev_database_config_quarantines_invalid_json_types(monkeypatch, tmp_path):
    config_path = isolate_dev_database_config(monkeypatch, tmp_path)
    invalid_payloads = [
        {"active_baza_dir": ["C:/wrong"], "saved_baza_dirs": []},
        {"active_baza_dir": None, "saved_baza_dirs": 1},
        {"active_baza_dir": None, "saved_baza_dirs": [123]},
    ]

    for payload in invalid_payloads:
        config_path.write_text(json.dumps(payload), encoding="utf-8")
        loaded = runtime_paths.read_dev_database_config()
        assert loaded["active_baza_dir"] is None
        assert loaded["saved_baza_dirs"] == []
        assert loaded.get("load_error")
        assert not config_path.exists()


def test_dev_database_config_lock_is_removed_if_token_write_fails(monkeypatch, tmp_path):
    config_path = isolate_dev_database_config(monkeypatch, tmp_path)

    def fail_write(_fd, _payload):
        raise OSError("simulated token write failure")

    monkeypatch.setattr(runtime_paths.os, "write", fail_write)

    with pytest.raises(runtime_paths.DataPathConfigurationError, match="заблокировать"):
        runtime_paths.read_dev_database_config()

    assert not Path(f"{config_path}.lock").exists()


def test_validate_dev_baza_dir_checks_real_sqlite_database(tmp_path):
    valid_root = create_baza(tmp_path / "valid")
    missing_root = tmp_path / "missing"
    missing_root.mkdir()

    assert "database" not in runtime_paths.REQUIRED_BAZA_DIRS
    assert not (valid_root / "database").exists()
    assert runtime_paths.validate_dev_baza_dir(str(valid_root)) == (True, "ok")
    ok, message = runtime_paths.validate_dev_baza_dir(str(missing_root))
    assert not ok
    assert "rao_journal.db" in message


def test_validate_dev_baza_dir_rejects_incomplete_runtime_structure(tmp_path):
    root = tmp_path / "incomplete"
    db_path = root / "archiv" / "rao_journal.db"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE patients (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE admissions (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE beds (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    settings_path = root / "settings" / "remcard_settings.db"
    settings_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(settings_path)
    try:
        conn.execute("CREATE TABLE settings_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
    finally:
        conn.close()

    ok, message = runtime_paths.validate_dev_baza_dir(str(root))

    assert not ok
    assert "служебных каталогов" in message


def test_switch_dialog_saves_paths_and_changes_active_database(monkeypatch, tmp_path):
    app = application()
    isolate_dev_database_config(monkeypatch, tmp_path)
    monkeypatch.delenv("REMCARD_BAZA_DIR", raising=False)
    first = create_baza(tmp_path / "first")
    second = create_baza(tmp_path / "second")
    runtime_paths.write_dev_database_config(str(first), [str(first)])

    dialog = DevDatabaseSwitchDialog()
    assert dialog.current_path == normalized(first)
    assert dialog.save_path_button.text() == "Добавить в список"
    dialog.path_edit.setText(str(second))
    dialog._save_path()
    wait_for_dialog_validation(app, dialog)

    saved_config = runtime_paths.read_dev_database_config()
    assert saved_config["active_baza_dir"] == normalized(first)
    assert normalized(second) in saved_config["saved_baza_dirs"]

    dialog._apply()
    wait_for_dialog_validation(app, dialog)
    assert dialog.result() == QDialog.Accepted
    assert dialog.active_changed is True
    assert dialog.selected_path == normalized(second)
    assert runtime_paths.get_dev_baza_dir() == normalized(second)
    dialog.deleteLater()
    app.processEvents()


def test_switch_dialog_rejects_folder_without_database(monkeypatch, tmp_path):
    app = application()
    isolate_dev_database_config(monkeypatch, tmp_path)
    monkeypatch.delenv("REMCARD_BAZA_DIR", raising=False)
    active = create_baza(tmp_path / "active")
    invalid = tmp_path / "invalid"
    invalid.mkdir()
    runtime_paths.write_dev_database_config(str(active), [str(active)])
    warnings = []
    monkeypatch.setattr(
        CustomMessageBox,
        "warning",
        lambda _parent, title, message: warnings.append((title, message)),
    )

    dialog = DevDatabaseSwitchDialog()
    dialog.path_edit.setText(str(invalid))
    dialog._apply()
    wait_for_dialog_validation(app, dialog)

    assert dialog.result() == QDialog.Rejected
    assert runtime_paths.get_dev_baza_dir() == normalized(active)
    assert warnings and "rao_journal.db" in warnings[0][1]
    dialog.deleteLater()
    app.processEvents()


def test_switch_dialog_validates_slow_network_path_outside_gui_thread(monkeypatch, tmp_path):
    app = application()
    isolate_dev_database_config(monkeypatch, tmp_path)
    monkeypatch.delenv("REMCARD_BAZA_DIR", raising=False)
    first = create_baza(tmp_path / "first")
    second = create_baza(tmp_path / "second")
    runtime_paths.write_dev_database_config(str(first), [str(first), str(second)])
    started = threading.Event()
    release = threading.Event()
    real_validate = runtime_paths.validate_dev_baza_dir

    def slow_validate(path):
        started.set()
        if not release.wait(2.0):
            return False, "timeout"
        return real_validate(path)

    monkeypatch.setattr(switch_dialog_module, "validate_dev_baza_dir", slow_validate)
    dialog = DevDatabaseSwitchDialog()
    dialog.path_edit.setText(str(second))

    validation_started_at = time.monotonic()
    dialog._apply()
    elapsed = time.monotonic() - validation_started_at

    assert elapsed < 0.5
    assert started.wait(1.0)
    assert dialog._validation_worker is not None
    dialog.reject()
    release.set()
    wait_for_dialog_validation(app, dialog)
    assert runtime_paths.read_dev_database_config()["active_baza_dir"] == normalized(first)
    dialog.deleteLater()
    app.processEvents()


def test_dev_process_pins_startup_database_and_drops_inherited_pin(monkeypatch, tmp_path):
    isolate_dev_database_config(monkeypatch, tmp_path)
    monkeypatch.setattr(app_main, "is_compiled", lambda: False)
    monkeypatch.delenv("REMCARD_BAZA_DIR", raising=False)
    monkeypatch.delenv(runtime_paths.DEV_RUNTIME_BAZA_PIN_ENV, raising=False)
    monkeypatch.delenv(runtime_paths.DEV_EXISTING_BAZA_ONLY_ENV, raising=False)
    first = create_baza(tmp_path / "first")
    second = create_baza(tmp_path / "second")
    runtime_paths.write_dev_database_config(str(first), [str(first), str(second)])

    assert app_main._configure_dev_runtime_baza_pin() == normalized(first)
    assert os.environ["REMCARD_BAZA_DIR"] == normalized(first)
    assert os.environ[runtime_paths.DEV_RUNTIME_BAZA_PIN_ENV] == str(os.getpid())
    assert os.environ[runtime_paths.DEV_EXISTING_BAZA_ONLY_ENV] == "1"

    runtime_paths.save_dev_baza_dir(str(second))
    assert runtime_paths.resolve_baza_dir() == normalized(first)

    monkeypatch.setenv(runtime_paths.DEV_RUNTIME_BAZA_PIN_ENV, "999999")
    assert app_main._configure_dev_runtime_baza_pin() == normalized(second)
    assert os.environ["REMCARD_BAZA_DIR"] == normalized(second)
    assert os.environ[runtime_paths.DEV_RUNTIME_BAZA_PIN_ENV] == str(os.getpid())


def test_save_only_does_not_change_database_after_dev_restart(monkeypatch, tmp_path):
    isolate_dev_database_config(monkeypatch, tmp_path)
    monkeypatch.setattr(app_main, "is_compiled", lambda: False)
    project_root = tmp_path / "project"
    fallback = create_baza(project_root / runtime_paths.DEFAULT_DEV_DATA_ROOT_NAME)
    candidate = create_baza(tmp_path / "network_database")
    monkeypatch.setattr(runtime_paths, "get_project_root", lambda: str(project_root))
    monkeypatch.delenv("REMCARD_BAZA_DIR", raising=False)
    monkeypatch.delenv(runtime_paths.DEV_RUNTIME_BAZA_PIN_ENV, raising=False)
    monkeypatch.delenv(runtime_paths.DEV_EXISTING_BAZA_ONLY_ENV, raising=False)

    assert app_main._configure_dev_runtime_baza_pin() == normalized(fallback)
    runtime_paths.add_saved_dev_baza_dir(str(candidate))
    monkeypatch.setenv(runtime_paths.DEV_RUNTIME_BAZA_PIN_ENV, "previous-process")

    assert app_main._configure_dev_runtime_baza_pin() == normalized(fallback)
    assert runtime_paths.read_dev_database_config() == {
        "active_baza_dir": None,
        "saved_baza_dirs": [normalized(candidate)],
    }
    assert runtime_paths.DEV_EXISTING_BAZA_ONLY_ENV not in os.environ


def test_dev_startup_accepts_saved_baza_without_legacy_database_dir(monkeypatch, tmp_path):
    isolate_dev_database_config(monkeypatch, tmp_path)
    monkeypatch.setattr(app_main, "is_compiled", lambda: False)
    monkeypatch.delenv("REMCARD_BAZA_DIR", raising=False)
    monkeypatch.delenv(runtime_paths.DEV_RUNTIME_BAZA_PIN_ENV, raising=False)
    monkeypatch.delenv(runtime_paths.DEV_EXISTING_BAZA_ONLY_ENV, raising=False)
    selected = create_baza(tmp_path / "selected")
    assert not (selected / "database").exists()
    runtime_paths.write_dev_database_config(str(selected), [str(selected)])

    assert app_main._configure_dev_runtime_baza_pin() == normalized(selected)
    assert os.environ[runtime_paths.DEV_EXISTING_BAZA_ONLY_ENV] == "1"


def test_dev_startup_refuses_missing_saved_database_without_creating_it(monkeypatch, tmp_path):
    isolate_dev_database_config(monkeypatch, tmp_path)
    monkeypatch.setattr(app_main, "is_compiled", lambda: False)
    monkeypatch.delenv("REMCARD_BAZA_DIR", raising=False)
    monkeypatch.delenv(runtime_paths.DEV_RUNTIME_BAZA_PIN_ENV, raising=False)
    missing = tmp_path / "deleted_database"
    runtime_paths.write_dev_database_config(str(missing), [str(missing)])

    with pytest.raises(runtime_paths.DataPathConfigurationError, match="Сохранённая dev-база"):
        app_main._configure_dev_runtime_baza_pin()

    assert not missing.exists()


def test_dev_startup_stops_after_broken_selection_instead_of_silent_fallback(monkeypatch, tmp_path):
    config_path = isolate_dev_database_config(monkeypatch, tmp_path)
    monkeypatch.setattr(app_main, "is_compiled", lambda: False)
    monkeypatch.delenv("REMCARD_BAZA_DIR", raising=False)
    monkeypatch.delenv(runtime_paths.DEV_RUNTIME_BAZA_PIN_ENV, raising=False)
    config_path.write_text("{broken json", encoding="utf-8")

    with pytest.raises(runtime_paths.DataPathConfigurationError, match="Запуск остановлен"):
        app_main._configure_dev_runtime_baza_pin()

    assert "REMCARD_BAZA_DIR" not in os.environ
    assert not config_path.exists()
    assert list(tmp_path.glob("dev_database_paths.json.broken.*"))


def test_saved_dev_database_connections_never_recreate_deleted_files(monkeypatch, tmp_path):
    root = create_baza(tmp_path / "selected")
    journal_path = root / "archiv" / "rao_journal.db"
    settings_path = root / "settings" / "remcard_settings.db"
    monkeypatch.setattr(runtime_paths, "is_compiled", lambda: False)
    monkeypatch.setenv("REMCARD_BAZA_DIR", str(root))
    monkeypatch.setenv(runtime_paths.DEV_EXISTING_BAZA_ONLY_ENV, "1")

    journal_path.unlink()
    with pytest.raises(FileNotFoundError, match="Сохранённая dev-база"):
        DatabaseManager(str(journal_path), str(journal_path))
    assert not journal_path.exists()

    settings_path.unlink()
    settings_db = SettingsDatabase(baza_dir=str(root))
    with pytest.raises(SettingsDbError, match="dev-база настроек"):
        settings_db.ensure_ready()
    assert not settings_path.exists()


def test_admin_settings_show_database_switch_only_in_dev(monkeypatch):
    app = application()

    monkeypatch.setattr(runtime_paths, "is_compiled", lambda: False)
    dev_widget = AdminMainWidget(role="admin")
    assert dev_widget.btn_switch_database.text() == "Смена базы"
    assert any(
        entry["button"] is dev_widget.btn_switch_database
        for entry in dev_widget.settings_action_cards
    )
    assert not dev_widget.btn_switch_database.isHidden()
    dev_widget.deleteLater()
    app.processEvents()

    monkeypatch.setattr(runtime_paths, "is_compiled", lambda: True)
    compiled_widget = AdminMainWidget(role="admin")
    assert compiled_widget.btn_switch_database.text() == "Смена базы"
    assert not any(
        entry["button"] is compiled_widget.btn_switch_database
        for entry in compiled_widget.settings_action_cards
    )
    assert compiled_widget.btn_switch_database.isHidden()
    compiled_widget.deleteLater()
    app.processEvents()
